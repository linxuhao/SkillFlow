"""An unsatisfiable validator must not destroy the step's work.

Validation retries share the step's retry budget; on exhaustion the step used to
fail permanently, which meant ``{step}.tmp/`` was never promoted and everything the
step had staged was deleted with it.

Measured on an autopep8 benchmark task: the card required reproducing upstream's
``test/`` directory VERBATIM — including ``test/bad_encoding2.py`` (deliberately
invalid encoding) and ``test/suite/E*.py`` (deliberate PEP8 violations) — under a
``{files: ["*.py"], tool: lint}`` gate. The gate was unsatisfiable by construction:
16 attempts across 4 step instances, 145 minutes, 36.6M prompt tokens, 74 correct
files written, ZERO bytes delivered. The feedback channel worked fine; telling an
agent "this file has a syntax error" simply cannot help when the task requires that
file to have a syntax error.

So the last attempt now promotes and flags instead of failing: the graph — not the
linter — decides what an unsatisfied validator means for this step.
"""
from pathlib import Path

import pytest

from mocks import MockToolLoader
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    EndCondition, EndConditions, PipelineGraph, StepNode, Transition,
)
from skillflow.workspace import WorkspaceManager

# A fixture the task REQUIRES to be broken — the linter can never pass on it.
REQUIRED_BROKEN_FIXTURE = "def broken(:\n"
GOOD_FILE = "VALUE = 42\n"


def _lint(files: list[str], workspace_root: str = "", **kwargs) -> dict:
    """Stand-in for the `lint` tool: compiles every match, reports each failure.

    Real ruff, but hermetic — the point of these tests is what the ENGINE does
    with a verdict it cannot satisfy, not how the verdict is reached.
    """
    root = Path(workspace_root)
    results = []
    for pattern in files:
        for fp in sorted(root.rglob(pattern)):
            try:
                compile(fp.read_text(), str(fp), "exec")
            except SyntaxError as e:
                results.append({"file": fp.name, "passed": False,
                                "error": f"{fp.name}: SyntaxError: {e.msg}"})
    return {"all_passed": not results, "results": results}


def _graph(name: str, transitions: list[Transition],
           end_conditions: EndConditions | None = None) -> PipelineGraph:
    targets = [t.to for t in transitions]
    return PipelineGraph(name=name, begin="impl", end_conditions=end_conditions,
                         steps=[
        StepNode(id="impl", step_type="agent", output_mode="write", max_retries=1,
                 validation=[{"files": ["*.py"], "tool": "lint"}],
                 transitions=transitions),
    ] + [
        StepNode(id=t, step_type="agent", transitions=[Transition(to=None)])
        for t in ("review", "repair") if t in targets
    ])


def _sf(tmp_path: Path, graph: PipelineGraph) -> SkillFlow:
    sf = SkillFlow(":memory:")
    sf.register_graph(graph)
    sf._tool_loader = MockToolLoader({"lint": _lint})
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"))
    return sf


def _stage_and_confirm(sf: SkillFlow, rid: str) -> None:
    """Claim `impl`, stage the good file next to the required-broken one, confirm."""
    claimed = sf.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == "impl", "impl was not claimable"
    tmp = sf._workspace.get_step_tmp_dir("pid", sf._get_graph_name(rid), "impl")
    (tmp / "ok.py").write_text(GOOD_FILE)
    (tmp / "bad_encoding2.py").write_text(REQUIRED_BROKEN_FIXTURE)
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))


def _start(sf: SkillFlow, name: str) -> str:
    rid = sf.create_run(name, {"project_id": "pid"})
    sf.start_run(rid)
    return rid


def _step_row(sf: SkillFlow, rid: str, step_id: str = "impl"):
    return sf._conn.execute(
        "SELECT status, last_error, result_flags_json, validation_retry_count "
        "FROM skillflow_steps WHERE run_id = ? AND step_id = ? ORDER BY id DESC LIMIT 1",
        (rid, step_id),
    ).fetchone()


def test_an_early_failure_still_just_retries(tmp_path):
    """Nothing about the non-final attempts changes: back to pending, error in hand."""
    sf = _sf(tmp_path, _graph("early", [Transition(to="review")]))
    rid = _start(sf, "early")
    _stage_and_confirm(sf, rid)

    row = _step_row(sf, rid)
    assert row["status"] == "pending", "an early failure must re-run the step"
    assert row["validation_retry_count"] == 1

    again = sf.claim_next_step(rid)
    assert "bad_encoding2.py" in str(again.validation_error), (
        "the retry lost the validator's feedback")
    step_dir = sf._workspace.get_step_dir("pid", "early", "impl")
    assert not step_dir.exists(), "an early failure must NOT promote anything"


def test_the_exhausted_attempt_promotes_and_flags(tmp_path):
    sf = _sf(tmp_path, _graph("exh", [Transition(to="review")]))
    rid = _start(sf, "exh")
    _stage_and_confirm(sf, rid)   # attempt 1 → retry
    _stage_and_confirm(sf, rid)   # attempt 2 → budget spent

    row = _step_row(sf, rid)
    assert row["status"] == "completed", (
        "the exhausted attempt must complete, not fail — failing is what deleted "
        "74 correct files")
    assert sf._deserialize(row["result_flags_json"]).get("validation_failed") is True


def test_the_promoted_content_is_actually_on_disk(tmp_path):
    """The whole point: the correct files survive the objection."""
    sf = _sf(tmp_path, _graph("disk", [Transition(to="review")]))
    rid = _start(sf, "disk")
    _stage_and_confirm(sf, rid)
    _stage_and_confirm(sf, rid)

    step_dir = sf._workspace.get_step_dir("pid", "disk", "impl")
    assert (step_dir / "ok.py").read_text() == GOOD_FILE
    assert (step_dir / "bad_encoding2.py").read_text() == REQUIRED_BROKEN_FIXTURE, (
        "the file the validator objected to is the one the task required")
    assert not (step_dir.parent / "impl.tmp").exists(), (
        "staging should have been renamed, not copied")


def test_the_flag_is_matchable_by_a_transition(tmp_path):
    """A graph that wants a human/reviewer verdict routes on the flag."""
    sf = _sf(tmp_path, _graph("routed", [
        Transition(to="repair", match={"validation_failed": True}),
        Transition(to="review"),
    ]))
    rid = _start(sf, "routed")
    _stage_and_confirm(sf, rid)
    _stage_and_confirm(sf, rid)

    node = sf._conn.execute(
        "SELECT current_node FROM skillflow_runs WHERE id = ?", (rid,)).fetchone()
    assert node["current_node"] == "repair"


def test_an_unrouted_graph_falls_through_but_says_so_loudly(tmp_path, caplog):
    """No matching transition → the normal edge, NOT a silent pass.

    Fall-through is deliberate: hard-failing here would keep destroying the work of
    every graph written before this fix. What must never happen is the failure
    VANISHING — so it lands in the flags, the trace, the notification bus, the step
    row's last_error and the log.
    """
    sf = _sf(tmp_path, _graph("fallthrough", [Transition(to="review")]))
    rid = _start(sf, "fallthrough")
    _stage_and_confirm(sf, rid)
    with caplog.at_level("WARNING", logger="skillflow"):
        _stage_and_confirm(sf, rid)

    node = sf._conn.execute(
        "SELECT current_node FROM skillflow_runs WHERE id = ?", (rid,)).fetchone()
    assert node["current_node"] == "review", "the run must keep going"

    events = [r["event"] for r in sf.get_trace(rid)]
    assert "validation_exhausted" in events, "the trace lost the failure"
    published = [r["event_type"] for r in sf._conn.execute(
        "SELECT event_type FROM skillflow_outbox").fetchall()]
    assert "step_validation_exhausted" in published, "the bus lost the failure"
    assert "bad_encoding2.py" in (_step_row(sf, rid)["last_error"] or "")
    assert "validation_failed=true" in caplog.text


def test_a_graph_can_still_demand_the_hard_stop(tmp_path):
    """The escape hatch, without a new config knob: match the flag in end_conditions."""
    sf = _sf(tmp_path, _graph("strict", [Transition(to="review")], end_conditions=(
        EndConditions(combinator="or", conditions=[
            EndCondition(type="flag_match", flag={"validation_failed": True}),
        ]))))
    rid = _start(sf, "strict")
    _stage_and_confirm(sf, rid)
    _stage_and_confirm(sf, rid)
    sf.advance_run(rid)

    run = sf._conn.execute(
        "SELECT status FROM skillflow_runs WHERE id = ?", (rid,)).fetchone()
    assert run["status"] == "failed"
    assert (sf._workspace.get_step_dir("pid", "strict", "impl") / "ok.py").exists(), (
        "even a hard stop keeps the work now")


def test_an_output_schema_failure_still_fails_hard(tmp_path):
    """Malformed step OUTPUTS have no reviewer in between — downstream context
    resolution reads them directly, so exhaustion stays a permanent failure."""
    sf = _sf(tmp_path, PipelineGraph(name="schema", begin="impl", steps=[
        StepNode(id="impl", step_type="agent", max_retries=1,
                 output_schema="nonexistent.pkg.Model", output_schema_retries=2,
                 transitions=[Transition(to="review")]),
        StepNode(id="review", step_type="agent", transitions=[Transition(to=None)]),
    ]))
    rid = _start(sf, "schema")
    for _ in range(2):
        claimed = sf.claim_next_step(rid)
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    row = _step_row(sf, rid)
    assert row["status"] == "failed"
    assert "validation_failed" not in (row["result_flags_json"] or "")


@pytest.mark.parametrize("budget", [1, 2, 3])
def test_only_the_last_attempt_promotes(tmp_path, budget):
    """Whatever the budget, every attempt before the last behaves as it always did."""
    sf = _sf(tmp_path, PipelineGraph(name=f"b{budget}", begin="impl", steps=[
        StepNode(id="impl", step_type="agent", output_mode="write",
                 max_retries=budget,
                 validation=[{"files": ["*.py"], "tool": "lint"}],
                 transitions=[Transition(to="review")]),
        StepNode(id="review", step_type="agent", transitions=[Transition(to=None)]),
    ]))
    rid = _start(sf, f"b{budget}")
    for attempt in range(budget):
        _stage_and_confirm(sf, rid)
        assert _step_row(sf, rid)["status"] == "pending", (
            f"attempt {attempt + 1} of {budget} promoted too early")
        assert not sf._workspace.get_step_dir("pid", f"b{budget}", "impl").exists()

    _stage_and_confirm(sf, rid)
    assert _step_row(sf, rid)["status"] == "completed"
    assert (sf._workspace.get_step_dir("pid", f"b{budget}", "impl") / "ok.py").exists()
