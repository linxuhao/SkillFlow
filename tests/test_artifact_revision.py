"""Behavioral tests for complete, instance-owned artifact candidates."""
import json
from pathlib import Path

import skillflow
from skillflow.core import SkillFlow, StepResult
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader

FIXED = {"card": "tasks/*.json", "manifest": "tasks_manifest.json"}


def engine(tmp_path, *, retries=2, fixed=None, validation=None):
    repo = tmp_path / "repo"
    repo.mkdir(exist_ok=True)
    sf = SkillFlow(str(tmp_path / "state.db"),
                   workspace_base=str(tmp_path / "artifacts"),
                   code_path_resolver=lambda pid, run_id=None: repo,
                   tool_loader=ToolLoader(Path(skillflow.__file__).parent / "tools"))
    node = StepNode(id="cards", step_type="agent", output_mode="content",
                    output_fixed=FIXED if fixed is None else fixed,
                    output_carry_forward=True, checkpoint=True, max_retries=retries,
                    context=[{"from": "repository", "mode": "tool"}],
                    validation=validation or [], transitions=[Transition(to="done")])
    sf.register_graph(PipelineGraph(name="revision", begin="cards",
                                   steps=[node, StepNode(id="done", step_type="agent")]))
    rid = sf.create_run("revision", project_id="p")
    sf.start_run(rid)
    sf.advance_run(rid)
    return sf, rid, sf.claim_next_step(rid)


def call(sf, rid, claim, name, **params):
    return sf.execute_tool(name, params, run_id=rid, step_id=claim.step_id,
                           step_instance_id=claim.token.step_instance_id,
                           claim_epoch=claim.token.claim_epoch)


def write_set(sf, rid, claim, names=("A", "B", "C")):
    for name in names:
        result = call(sf, rid, claim, "write_card", id=name,
                      content=json.dumps({"id": name, "value": 1}))
        assert "error" not in result, result
    result = call(sf, rid, claim, "write_manifest",
                  content=json.dumps({"execution_order": [list(names)]}))
    assert "error" not in result, result


def revise(sf, rid, claim):
    sf.confirm_step(claim.token, StepResult())
    sf.advance_run(rid)
    assert sf.get_run(rid)["status"] == "paused"
    sf.reject_checkpoint(rid, "cards", "Revise only the requested files.")
    return sf.claim_next_step(rid)


def files(root):
    return {str(p.relative_to(root)): p.read_bytes()
            for p in root.rglob("*") if p.is_file()}


def test_edit_one_card_publishes_complete_set_once(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    prior = Path(claim.inputs["_artifact_dir"])
    before = files(prior)
    result = call(sf, rid, claim, "edit_card", id="A", old_str='"value": 1', new_str='"value": 2')
    assert "error" not in result, result
    sf.confirm_step(claim.token, StepResult())
    after = files(prior)
    assert set(after) == set(before)
    assert after["tasks/B.json"] == before["tasks/B.json"]
    assert after["tasks/C.json"] == before["tasks/C.json"]
    assert json.loads(after["tasks/A.json"])["value"] == 2


def test_deleted_card_is_absent_from_current_reads_and_edits(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    result = call(sf, rid, claim, "delete_card", id="B")
    assert result.get("deleted"), result
    result = call(sf, rid, claim, "read", path="tasks/B.json", source="self")
    assert "error" in result, result
    result = call(sf, rid, claim, "edit_card", id="B", old_str='"value": 1', new_str='"value": 3')
    assert "error" in result, result
    assert not (Path(claim.inputs["_output_dir"]) / "tasks/B.json").exists()


def test_empty_candidate_survives_reclaim_and_publishes_empty_set(tmp_path):
    sf, rid, claim = engine(tmp_path, fixed={"card": "*.json"})
    assert "error" not in call(sf, rid, claim, "write_card", id="A", content="{}")
    claim = revise(sf, rid, claim)
    assert call(sf, rid, claim, "delete_card", id="A").get("deleted")
    candidate = Path(claim.inputs["_output_dir"])
    assert files(candidate) == {}
    sf.release_claim(claim.token, "simulate host restart")
    claim = sf.claim_next_step(rid)
    assert files(candidate) == {}
    sf.confirm_step(claim.token, StepResult())
    assert files(Path(claim.inputs["_artifact_dir"])) == {}


def test_new_run_does_not_inherit_previous_runs_artifacts(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    sf.confirm_step(claim.token, StepResult())
    other = sf.create_run("revision", project_id="p")
    sf.start_run(other)
    sf.advance_run(other)
    new = sf.claim_next_step(other)
    assert files(Path(new.inputs["_output_dir"])) == {}
    assert "error" in call(sf, other, new, "read", path="tasks/A.json", source="self")


def test_failed_validation_leaves_publication_intact(tmp_path):
    sf, rid, claim = engine(tmp_path, retries=0,
                           validation=[{"tool": "file_exists", "files": ["tasks/B.json"]}])
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    prior = Path(claim.inputs["_artifact_dir"])
    before = files(prior)
    assert call(sf, rid, claim, "delete_card", id="B").get("deleted")
    sf.confirm_step(claim.token, StepResult())
    assert files(prior) == before
    row = sf._conn.execute("SELECT status FROM skillflow_steps WHERE id=?",
                           (claim.token.step_instance_id,)).fetchone()
    assert row["status"] != "completed"
    assert not (Path(claim.inputs["_output_dir"]) / "tasks/B.json").exists()


def test_validation_retry_preserves_changes_and_deletions(tmp_path):
    sf, rid, claim = engine(tmp_path,
                           validation=[{"tool": "file_exists", "files": ["tasks/B.json"]}])
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    assert call(sf, rid, claim, "delete_card", id="B").get("deleted")
    assert "error" not in call(sf, rid, claim, "write_card", id="D", content='{"new":true}')
    sf.confirm_step(claim.token, StepResult())
    new = sf.claim_next_step(rid)
    assert new.token.step_instance_id == claim.token.step_instance_id
    candidate = Path(new.inputs["_output_dir"])
    assert not (candidate / "tasks/B.json").exists()
    assert (candidate / "tasks/D.json").read_text() == '{"new":true}'
    assert (candidate / "tasks/C.json").is_file()


def test_no_change_revision_keeps_bytes(tmp_path):
    sf, rid, claim = engine(tmp_path)
    write_set(sf, rid, claim)
    claim = revise(sf, rid, claim)
    prior = Path(claim.inputs["_artifact_dir"])
    before = files(prior)
    sf.confirm_step(claim.token, StepResult())
    assert files(prior) == before
