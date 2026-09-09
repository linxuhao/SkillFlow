"""A goal-loop failure must not reopen an earlier completed pass."""
import json
import pytest
from skillflow.graph import PipelineGraph, StepNode


def setup_run(sf):
    sf.register_graph(PipelineGraph(name="recover", begin="3_review",
                                   steps=[StepNode(id="3_review", step_type="agent")]))
    run = sf.create_run("recover")
    sf.start_run(run)
    sf.advance_run(run)
    with sf._conn:
        sf._conn.execute("UPDATE skillflow_steps SET status='completed', outputs_json=?, completion_seq=1 WHERE run_id=?",
                         (json.dumps({"old_card_output": "preserve"}), run))
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status,outputs_json) VALUES (4480,?,'t_impl','failed','{}')", (run,))
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status,outputs_json) VALUES (4481,?,'t_impl','completed',?)", (run,json.dumps({"finished_card":"keep"})))
    return run


def rows(sf, run):
    return [dict(r) for r in sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=? ORDER BY id", (run,))]


def test_native_failure_reopens_only_current_instance_with_budget(sf):
    run = setup_run(sf)
    with sf._conn:
        sf._conn.execute("INSERT INTO skillflow_steps (id,run_id,step_id,status,retry_count,validation_retry_count,inputs_json,claim_epoch) VALUES (4491,?,'3_review','failed',3,2,?,7)",
                         (run,json.dumps({"_validation_error":"old", "keep":"input"})))
        sf._conn.execute("UPDATE skillflow_runs SET status='failed',current_node='3_review',error_reason='Step 3_review: native turn budget exhausted' WHERE id=?",(run,))
    before = rows(sf, run)
    sf.reactivate_run(run)
    after = rows(sf, run)
    assert after[:-1] == before[:-1]
    failed = after[-1]
    assert failed["id"] == 4491 and failed["status"] == "pending"
    assert failed["retry_count"] == failed["validation_retry_count"] == 0
    assert failed["claim_epoch"] == 7
    assert json.loads(failed["inputs_json"]) == {"keep":"input"}
    assert sf.get_run(run)["current_node"] == "3_review"
    assert sf.get_run(run)["status"] == "running"


@pytest.mark.parametrize("status,error", [("paused", ""), ("failed", "No matching transition from '3_review' with flags {}")])
def test_intentional_completed_step_recovery_preserved(sf, status, error):
    run = setup_run(sf)
    with sf._conn:
        sf._conn.execute("UPDATE skillflow_runs SET status=?,current_node='3_review',error_reason=? WHERE id=?",(status,error,run))
    before = rows(sf,run)
    sf.reactivate_run(run)
    after = rows(sf,run)
    assert after[0]["status"] == "pending"
    assert after[1:] == before[1:]


def test_normal_resume_does_not_reopen_completed_checkpoint(sf):
    run = setup_run(sf)
    with sf._conn:
        sf._conn.execute("UPDATE skillflow_runs SET status='paused',current_node='3_review' WHERE id=?",(run,))
    before = rows(sf,run)
    sf.resume_run(run)
    assert rows(sf,run) == before
