"""A retry owns its producer position even when an older checkpoint completed."""
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import OutputValidationError, SkillFlowError
from skillflow.graph import PipelineGraph, StepNode, Transition


def flow(tmp_path):
    sf = SkillFlow(str(tmp_path / "state.db"), workspace_base=str(tmp_path / "ws"))
    sf.register_graph(PipelineGraph(name="retry", begin="plan", steps=[
        StepNode(id="plan", output_mode="content",
                 output_fixed={"design": "design.md", "inventory": "inventory.json"},
                 checkpoint=True, transitions=[Transition(to="implement")]),
        StepNode(id="implement")]))
    rid = sf.create_run("retry", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    return sf, rid


def produce(sf, claim, design="original"):
    root = Path(claim.inputs["_output_dir"])
    (root / "design.md").write_text(design)
    (root / "inventory.json").write_text('{"version":1}')
    sf.confirm_step(claim.token, StepResult())


def row(sf, rid, iid):
    return next(r for r in sf.get_steps(rid) if r["id"] == iid)


@pytest.mark.parametrize("failure", ["missing-fixed-slot", "executor"])
def test_rejected_checkpoint_retry_confirms_new_producer(tmp_path, failure):
    sf, rid = flow(tmp_path)
    first = sf.claim_next_step(rid)
    produce(sf, first)
    sf.advance_run(rid)
    old = sf.get_checkpoint(rid)
    old_row = row(sf, rid, first.token.step_instance_id)
    old_trace = sf.get_trace(rid, step_instance_id=first.token.step_instance_id)
    sf.reject_checkpoint(rid, "plan", "Revise the design")
    revised = sf.claim_next_step(rid)
    root = Path(revised.inputs["_output_dir"])
    (root / "design.md").write_text("revised")
    if failure == "missing-fixed-slot":
        with pytest.raises(OutputValidationError, match="inventory") as refused:
            sf.confirm_step(revised.token, StepResult())
        error = str(refused.value)
    else:
        error = "executor failed before confirmation"
    sf.fail_step(revised.token, error, retryable=True)
    pending = row(sf, rid, revised.token.step_instance_id)
    assert pending["status"] == "pending"
    assert pending["completion_seq"] is None
    with sf._ro() as conn:
        assert conn.execute("SELECT outputs_json FROM skillflow_steps WHERE id=?", (revised.token.step_instance_id,)).fetchone()[0] == "{}"
    assert pending["retry_count"] == 1 and pending["last_error"] == error
    assert sf.advance_run(rid) == "plan"
    assert sf.get_run(rid)["status"] == "running"
    with pytest.raises(SkillFlowError, match="not paused"):
        sf.approve_checkpoint(rid, expected_checkpoint=old["expected_checkpoint"], decision_id="old-pending")
    again = sf.claim_next_step(rid)
    assert again.token.step_instance_id == revised.token.step_instance_id
    assert again.token.claim_epoch > revised.token.claim_epoch
    assert again.inputs["_feedback"] == revised.inputs["_feedback"]
    produce(sf, again, "revised")
    sf.advance_run(rid)
    current = sf.get_checkpoint(rid)
    assert current["expected_checkpoint"]["step_instance_id"] == revised.token.step_instance_id
    assert current["expected_checkpoint"]["completion_seq"] > old["expected_checkpoint"]["completion_seq"]
    assert current["outputs"] == {"design": "revised", "inventory": {"version": 1}}
    assert row(sf, rid, first.token.step_instance_id) == old_row
    assert sf.get_trace(rid, step_instance_id=first.token.step_instance_id) == old_trace
    with pytest.raises(SkillFlowError, match="Stale"):
        sf.approve_checkpoint(rid, expected_checkpoint=old["expected_checkpoint"], decision_id="old-revised")
    sf.approve_checkpoint(rid, expected_checkpoint=current["expected_checkpoint"], decision_id="current")
    sf.approve_checkpoint(rid, expected_checkpoint=current["expected_checkpoint"], decision_id="current")
    next_claim = sf.claim_next_step(rid)
    assert next_claim.step_id == "implement"


def test_noncheckpoint_failure_retries_the_same_instance(tmp_path):
    sf, rid = flow(tmp_path)
    first = sf.claim_next_step(rid)
    produce(sf, first)
    sf.advance_run(rid)
    cp = sf.get_checkpoint(rid)
    sf.approve_checkpoint(rid, expected_checkpoint=cp["expected_checkpoint"], decision_id="first")
    claim = sf.claim_next_step(rid)
    sf.fail_step(claim.token, "retry downstream executor", retryable=True)
    assert sf.advance_run(rid) == "implement"
    again = sf.claim_next_step(rid)
    assert again.token.step_instance_id == claim.token.step_instance_id
    sf.confirm_step(again.token, StepResult(outputs={"done": True}))
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "completed"
    sf.approve_checkpoint(rid, expected_checkpoint=cp["expected_checkpoint"], decision_id="first")
