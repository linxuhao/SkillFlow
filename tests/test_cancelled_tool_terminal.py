"""An admitted successful tool may drain, but cannot complete a cancelled run."""
import json
from concurrent.futures import ThreadPoolExecutor
from threading import Event

from skillflow.core import SkillFlow
from skillflow.graph import EndCondition, EndConditions, PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader


def _engine(tmp_path, tool):
    loader = ToolLoader()
    sf = SkillFlow(str(tmp_path / "owned-test.db"), tool_loader=loader)
    loader.register_dynamic_tool("owned_cpu", {"name": "owned_cpu", "parameters": {}}, tool)
    sf.register_agent_config("unused")
    sf.register_graph(PipelineGraph(
        name="normal_cpu", begin="test", steps=[
            StepNode(id="test", step_type="tool", tool_name="owned_cpu",
                     transitions=[Transition(to="done")]),
            StepNode(id="done", agent_config="unused", transitions=[]),
        ], end_conditions=EndConditions(conditions=[
            EndCondition(type="node_reached", node="done", result="completed")
        ])))
    return sf


def _run(sf, project_id="owned"):
    run_id = sf.create_run("normal_cpu", project_id=project_id)
    sf.start_run(run_id)
    return run_id


def _rows(sf, table, run_id):
    with sf._lock:
        return [dict(r) for r in sf._conn.execute(
            f"SELECT * FROM {table} WHERE run_id = ?", (run_id,)).fetchall()]


def _events(sf, run_id):
    with sf._lock:
        return [r["event_type"] for r in sf._conn.execute(
            "SELECT event_type, payload_json FROM skillflow_outbox").fetchall()
            if json.loads(r["payload_json"]).get("_run_id") == run_id]


def test_successful_tool_drains_without_completing_cancelled_run(tmp_path):
    entered, release = Event(), Event()
    completed = tmp_path / "owned-tool-result.txt"
    staged = tmp_path / "staged-output.txt"
    staged.write_text("preserve admitted output")
    calls = []
    owned = None

    def owned_cpu(run_id, **kwargs):
        calls.append(run_id)
        if run_id == owned:
            entered.set()
            assert release.wait(10), "test controller did not release owned tool"
            completed.write_text("actual CPU tool finished")
        return {"passed": True, "tests_passed": 1}

    sf = _engine(tmp_path, owned_cpu)
    owned = _run(sf)
    peer = _run(sf, "peer")
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(sf.advance_run, owned)
        try:
            assert entered.wait(10)
            operations = _rows(sf, "skillflow_active_ops", owned)
            claims = _rows(sf, "skillflow_steps", owned)
            assert len(operations) == 1
            assert operations[0]["owner"]
            assert claims[0]["status"] == "claimed"
            reason = "exact owned cancellation while normal tool executes"
            stop = sf.stop_run(owned, reason)
            assert stop["outcome"] == "draining"
            assert stop["status"] == "running"
            assert stop["admitted_operations"] == ["tool_step:owned_cpu"]
            assert list(stop["admitted_owner_state"].values()) == ["alive"]
            assert not stop["recovery_required"]
            requested = sf.get_run(owned)["cancel_requested_at"]
            assert requested
            assert not future.done()
            assert not completed.exists()
            assert _rows(sf, "skillflow_active_ops", owned) == operations
            assert _rows(sf, "skillflow_steps", owned)[0]["claimed_by"] == claims[0]["claimed_by"]
            assert _rows(sf, "skillflow_steps", owned)[0]["claim_epoch"] == claims[0]["claim_epoch"]
            sf.advance_run(peer)
            assert sf.get_run(peer)["status"] == "completed"
            assert _rows(sf, "skillflow_active_ops", owned) == operations
        finally:
            release.set()
        future.result(timeout=10)

    terminal = sf.get_run(owned)
    assert terminal["status"] == "failed", terminal
    assert terminal["error_reason"] == reason
    assert terminal["cancel_requested_at"] == requested
    assert terminal["completed_at"]
    assert _rows(sf, "skillflow_active_ops", owned) == []
    assert not any(r["status"] == "claimed" for r in _rows(sf, "skillflow_steps", owned))
    assert staged.read_text() == "preserve admitted output"
    assert completed.read_text() == "actual CPU tool finished"
    assert calls == [owned, peer]
    assert "run_completed" not in _events(sf, owned)
    assert _events(sf, owned).count("run_failed") == 1
    assert sf.get_run(peer)["status"] == "completed"


def test_completion_api_cannot_bypass_active_tool_drain(tmp_path):
    entered, release = Event(), Event()

    def owned_cpu(**kwargs):
        entered.set()
        assert release.wait(10)
        return {"passed": True}

    sf = _engine(tmp_path, owned_cpu)
    run_id = _run(sf)
    with ThreadPoolExecutor(max_workers=1) as executor:
        future = executor.submit(sf.advance_run, run_id)
        try:
            assert entered.wait(10)
            assert sf.stop_run(run_id, "keep cancellation reason")["outcome"] == "draining"
            operations = _rows(sf, "skillflow_active_ops", run_id)
            sf.complete_run(run_id)
            assert sf.get_run(run_id)["status"] == "running"
            assert _rows(sf, "skillflow_active_ops", run_id) == operations
            assert "run_completed" not in _events(sf, run_id)
        finally:
            release.set()
        future.result(timeout=10)
    assert sf.get_run(run_id)["status"] == "failed"
    assert sf.get_run(run_id)["error_reason"] == "keep cancellation reason"


def test_completion_that_wins_before_stop_remains_completed(tmp_path):
    sf = _engine(tmp_path, lambda **kwargs: {"passed": True})
    run_id = _run(sf)
    sf.advance_run(run_id)
    before = sf.get_run(run_id)
    assert before["status"] == "completed"
    assert sf.stop_run(run_id, "late stop")["outcome"] == "already_terminal"
    sf.complete_run(run_id)
    assert sf.get_run(run_id) == before
    assert _events(sf, run_id).count("run_completed") == 1


def test_completion_cannot_revive_already_cancelled_run(tmp_path):
    sf = _engine(tmp_path, lambda **kwargs: {"passed": True})
    run_id = _run(sf)
    assert sf.stop_run(run_id, "cancel before tool")["outcome"] == "stopped"
    before = sf.get_run(run_id)
    sf.complete_run(run_id)
    assert sf.get_run(run_id) == before
    assert "run_completed" not in _events(sf, run_id)
