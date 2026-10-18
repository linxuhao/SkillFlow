"""A real checkpoint capture refusal must survive the next normal claim."""
import json
from pathlib import Path

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.exceptions import StaleClaimFenced
from skillflow.graph import PipelineGraph, StepNode, Transition


def setup(tmp_path, budget=3):
    sf = SkillFlow(str(tmp_path / "state.db"), workspace_base=str(tmp_path / "ws"))
    sf.register_graph(PipelineGraph(name="capture", begin="plan", steps=[
        StepNode(id="plan", checkpoint=True, max_retries=budget,
                 context=[{"source": {"step": "plan", "mode": "tool"}}],
                 output_mode="content", output_fixed={"design": "design.md", "inventory": "inventory.json"},
                 transitions=[Transition(to="done")]), StepNode(id="done")]))
    rid = sf.create_run("capture", project_id="owned")
    sf.start_run(rid)
    first = sf.claim_next_step(rid)
    complete(sf, first)
    sf.advance_run(rid)
    old = sf.get_checkpoint(rid)
    sf.reject_checkpoint(rid, "plan", "Revise design; preserve inventory content")
    return sf, rid, old, sf.claim_next_step(rid)


def complete(sf, claim):
    root = Path(claim.inputs["_output_dir"])
    (root / "design.md").write_text("design")
    (root / "inventory.json").write_text('{"v":1}')
    sf.confirm_step(claim.token, StepResult())


def row(sf, rid, claim):
    return next(r for r in sf.get_steps(rid) if r["id"] == claim.step_instance_id)


@pytest.mark.parametrize("bad", ["missing", "malformed"])
def test_capture_refusal_reclaims_current_producer_without_delivery(tmp_path, monkeypatch, bad):
    sf, rid, old, claim = setup(tmp_path)
    root = Path(claim.inputs["_output_dir"])
    (root / "design.md").write_text("revised")
    if bad == "malformed":
        (root / "inventory.json").write_text("{bad json")
    assert (rid, "plan") in sf._step_tools
    # No delivery admission/hook is allowed before a complete captured set.
    with monkeypatch.context() as m:
        m.setattr(sf, "_admit_op", lambda *a, **k: pytest.fail("capture refusal admitted delivery"))
        m.setattr(sf, "_execute_lifecycle_hook", lambda *a, **k: pytest.fail("capture refusal ran hook"))
        sf.confirm_step(claim.token, StepResult())
    assert (rid, "plan") not in sf._step_tools
    pending = row(sf, rid, claim)
    assert pending["status"] == "pending" and pending["completion_seq"] is None
    assert pending["validation_retry_count"] == 1 and pending["retry_count"] == 0
    assert (root / "design.md").read_text() == "revised"
    assert sf.advance_run(rid) == "plan" and sf.get_run(rid)["status"] == "running"
    again = sf.claim_next_step(rid)
    assert again.step_instance_id == claim.step_instance_id
    assert again.token.claim_epoch > claim.token.claim_epoch
    assert "Checkpoint output 'inventory'" in again.validation_error
    assert "_validation_error" in again.inputs
    assert sf._get_resolver_for_run(rid).get_node("plan").output_carry_forward is False
    complete(sf, again)
    sf.advance_run(rid)
    current = sf.get_checkpoint(rid)
    assert current["expected_checkpoint"]["step_instance_id"] == again.step_instance_id
    assert current["expected_checkpoint"]["completion_seq"] > old["expected_checkpoint"]["completion_seq"]
    sf.reject_checkpoint(rid, "plan", "Another revision")
    fresh = sf.claim_next_step(rid)
    assert fresh.step_instance_id != again.step_instance_id
    assert fresh.validation_error is None


def test_each_later_refusal_replaces_prior_error_on_same_instance(tmp_path):
    sf, rid, old, claim = setup(tmp_path)
    root = Path(claim.inputs["_output_dir"])
    (root / "design.md").write_text("revised")
    sf.confirm_step(claim.token, StepResult())
    sf.advance_run(rid)
    second = sf.claim_next_step(rid)
    assert "No such file" in second.validation_error
    (root / "inventory.json").write_text("{malformed")
    sf.confirm_step(second.token, StepResult())
    sf.advance_run(rid)
    third = sf.claim_next_step(rid)
    assert third.step_instance_id == claim.step_instance_id
    assert "Expecting property name" in third.validation_error
    assert "No such file" not in third.validation_error
    (root / "inventory.json").write_text('{"v":1}')
    (root / "design.md").unlink()
    sf.confirm_step(third.token, StepResult())
    sf.advance_run(rid)
    fourth = sf.claim_next_step(rid)
    assert "Checkpoint output 'design'" in fourth.validation_error
    assert "Checkpoint output 'inventory'" not in fourth.validation_error
    complete(sf, fourth)
    sf.advance_run(rid)
    assert sf.get_checkpoint(rid)["expected_checkpoint"]["step_instance_id"] == claim.step_instance_id


def test_existing_combined_budget_hard_fails_incomplete_set(tmp_path):
    sf, rid, old, claim = setup(tmp_path, budget=2)
    sf.fail_step(claim.token, "executor unavailable", retryable=True)
    sf.advance_run(rid)
    second = sf.claim_next_step(rid)
    assert second.validation_error is None  # generic errors are not output refusals
    root = Path(second.inputs["_output_dir"])
    (root / "design.md").write_text("retained incomplete work")
    sf.confirm_step(second.token, StepResult())
    sf.advance_run(rid)
    third = sf.claim_next_step(rid)
    assert "inventory" in third.validation_error
    sf.confirm_step(third.token, StepResult())
    failed = row(sf, rid, third)
    assert failed["status"] == "failed" and failed["completion_seq"] is None
    assert failed["retry_count"] == 1 and failed["validation_retry_count"] == 1
    assert "Output validation failed" in failed["last_error"]
    assert sf.get_run(rid)["status"] == "failed"
    assert (root / "design.md").read_text() == "retained incomplete work"
    with sf._ro() as conn:
        assert json.loads(conn.execute("SELECT outputs_json FROM skillflow_steps WHERE id=?", (third.step_instance_id,)).fetchone()[0]) == {}


def test_claim_release_restart_has_no_fabricated_rejection_and_fences_old_confirm(tmp_path):
    sf, rid, old, claim = setup(tmp_path)
    root = Path(claim.inputs["_output_dir"])
    (root / "design.md").write_text("retained on restart")
    sf.release_claim(claim.token, reason="offline executor cancelled")
    sf.advance_run(rid)
    again = sf.claim_next_step(rid)
    assert again.validation_error is None
    assert row(sf, rid, again)["validation_retry_count"] == 0
    assert row(sf, rid, again)["retry_count"] == 0
    before = row(sf, rid, again)
    with pytest.raises(StaleClaimFenced):
        sf.confirm_step(claim.token, StepResult())
    assert row(sf, rid, again) == before
    complete(sf, again)
    sf.advance_run(rid)
    assert sf.get_checkpoint(rid)["outputs"]["inventory"] == {"v": 1}
