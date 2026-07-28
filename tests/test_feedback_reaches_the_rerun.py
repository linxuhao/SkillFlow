"""Feedback must reach the step that is re-run, not an earlier instance of it.

Both defects here only bite a step that runs MORE THAN ONCE — i.e. every maker in
a Green/Red loop after its first rejection — which is why they survived: a
single-instance step gets its feedback correctly, so the mechanism looks alive.

Measured on a real `pipeline_forge` run before the fix: `emit_graph` was claimed
seven times across four instances, three of them after an identical gate failure.
Not one of those instances carried `_feedback`, and the last — with
`validation_retry_count = 3` — resolved no validation-error context at all. In the
same run, `architect` (ONE instance) received its validation error exactly as
designed. That contrast is the whole bug.
"""
from unittest.mock import MagicMock

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition


def _loopback_graph():
    """impl → gate(tool, fails) --feedback--> impl."""
    return PipelineGraph(name="fb", begin="impl", steps=[
        StepNode(id="impl", step_type="agent",
                 transitions=[Transition(to="gate")]),
        StepNode(id="gate", step_type="tool", tool_name="validate",
                 transitions=[
                     Transition(to="done", match={"all_passed": True}),
                     Transition(to="impl", match={"all_passed": False},
                                max_loop=3, feedback=True),
                 ]),
        StepNode(id="done", step_type="agent"),
    ])


def _failing_tool():
    mock = MagicMock()
    mock.load_fn.return_value = lambda **kw: {
        "all_passed": False,
        "error": "3 tests failed: test_login, test_logout, test_refresh",
    }
    mock.load_schema.return_value = {"name": "validate"}
    return mock


def test_a_tool_gates_error_reaches_the_maker_it_loops_back_to():
    """`feedback: true` on a BACKWARD edge used to be a silent no-op.

    `_inject_feedback_in_tx` updated rows `WHERE status = 'pending'`, but the maker
    it routes back to is `completed` and its next instance does not exist yet — the
    claim path inserts it afterwards. The UPDATE matched zero rows and the maker
    re-ran with no idea why.
    """
    sf = SkillFlow(":memory:")
    sf.register_graph(_loopback_graph())
    sf._tool_loader = _failing_tool()
    rid = sf.create_run("fb")
    sf.start_run(rid)

    first = sf.claim_next_step(rid)
    assert first.step_id == "impl"
    assert not first.inputs.get("_feedback"), "nothing to feed back on the first run"
    sf.confirm_step(first.token, StepResult(outputs={}, flags={}))

    sf.advance_run(rid)          # resolve to the tool
    assert sf.advance_run(rid) == "impl"    # tool runs, routes back

    second = sf.claim_next_step(rid)
    assert second.step_id == "impl"
    assert "test_login" in str(second.inputs.get("_feedback")), (
        "the gate's error did not reach the re-run — the maker is about to repeat "
        "the same mistake")


def test_the_re_run_is_a_new_instance_and_still_gets_the_feedback():
    """Guards the carry-forward specifically: the fresh row starts blank."""
    sf = SkillFlow(":memory:")
    sf.register_graph(_loopback_graph())
    sf._tool_loader = _failing_tool()
    rid = sf.create_run("fb")
    sf.start_run(rid)

    first = sf.claim_next_step(rid)
    sf.confirm_step(first.token, StepResult(outputs={}, flags={}))
    sf.advance_run(rid)
    sf.advance_run(rid)
    second = sf.claim_next_step(rid)

    assert second.step_instance_id != first.step_instance_id, (
        "expected a fresh instance for the re-run")
    assert second.inputs.get("_feedback")


def test_validation_error_is_read_from_the_instance_being_claimed():
    """The claim path used to read `WHERE run_id = ? AND step_id = ?` with no
    ORDER BY and no LIMIT, so `fetchone()` returned the OLDEST instance — while
    `_handle_validation_failure` writes to the NEWEST.
    """
    sf = SkillFlow(":memory:")
    sf.register_graph(PipelineGraph(name="v", begin="mk", steps=[
        StepNode(id="mk", step_type="agent", transitions=[Transition(to="end")]),
        StepNode(id="end", step_type="agent"),
    ]))
    rid = sf.create_run("v")
    sf.start_run(rid)

    first = sf.claim_next_step(rid)
    sf.confirm_step(first.token, StepResult(outputs={}, flags={}))

    # A second instance, re-opened by a validation failure (what
    # _handle_validation_failure does: status back to pending + _validation_error).
    with sf._tx() as conn:
        conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "max_retries, status, inputs_json, created_at, updated_at) "
            "VALUES (?, 'mk', '{}', 3, 'pending', ?, datetime('now'), datetime('now'))",
            (rid, '{"_validation_error": "note.md was never written"}'))
        conn.execute("UPDATE skillflow_runs SET current_node = 'mk' WHERE id = ?",
                     (rid,))

    claimed = sf.claim_next_step(rid)
    assert claimed is not None and claimed.step_id == "mk"
    assert claimed.validation_error == "note.md was never written", (
        "read an older instance instead of the one being claimed")


def test_a_stale_validation_error_is_not_resurrected_on_a_loop_back():
    """`_validation_error` belongs to ONE instance's retry cycle. Carrying it onto a
    fresh instance would report a mistake the re-run has not made yet — so only
    `_feedback` is carried forward."""
    sf = SkillFlow(":memory:")
    sf.register_graph(_loopback_graph())
    sf._tool_loader = _failing_tool()
    rid = sf.create_run("fb")
    sf.start_run(rid)

    first = sf.claim_next_step(rid)
    with sf._tx() as conn:
        conn.execute(
            "UPDATE skillflow_steps SET inputs_json = "
            "json_set(inputs_json, '$._validation_error', 'old failure') WHERE id = ?",
            (first.step_instance_id,))
    sf.confirm_step(first.token, StepResult(outputs={}, flags={}))
    sf.advance_run(rid)
    sf.advance_run(rid)

    second = sf.claim_next_step(rid)
    assert second.validation_error is None
    assert second.inputs.get("_feedback"), "the feedback itself must still arrive"


@pytest.mark.parametrize("rounds", [1, 2, 3])
def test_every_round_carries_the_latest_error_not_the_first(rounds):
    """A maker looping several times must see the CURRENT round's error."""
    sf = SkillFlow(":memory:")
    sf.register_graph(_loopback_graph())
    seq = {"n": 0}

    def _tool(**kw):
        seq["n"] += 1
        return {"all_passed": False, "error": f"round {seq['n']} failure"}

    mock = MagicMock()
    mock.load_fn.return_value = _tool
    mock.load_schema.return_value = {"name": "validate"}
    sf._tool_loader = mock

    rid = sf.create_run("fb")
    sf.start_run(rid)
    claimed = sf.claim_next_step(rid)
    for r in range(1, rounds + 1):
        sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))
        sf.advance_run(rid)
        sf.advance_run(rid)
        claimed = sf.claim_next_step(rid)
        assert claimed is not None, f"run died at round {r}"
        assert f"round {r} failure" in str(claimed.inputs.get("_feedback"))
