"""Real child-owned claims: recovery may not reach beyond the selected run."""
import json
import subprocess
import sys

import pytest

from skillflow.core import SkillFlow
from skillflow.exceptions import SkillFlowError
from skillflow.identity import owner_is_dead
from skillflow.plugins.skill_runner.service import RunnerService


CHILD = r"""
import json, sys
from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
sf = SkillFlow(sys.argv[1])
sf.register_graph(PipelineGraph(name="scope", begin="a", steps=[
    StepNode(id="a", step_type="agent", transitions=[Transition(to=None)])]))
runs = []
for i in range(2):
    rid = sf.create_run("scope")
    sf.start_run(rid)
    sf.advance_run(rid)
    assert sf.claim_next_step(rid) is not None
    runs.append(rid)
print(json.dumps(runs), flush=True)
if len(sys.argv) > 2:
    sys.stdin.readline()
"""


@pytest.fixture
def owned_runs(tmp_path):
    db = str(tmp_path / "owned.db")
    child = subprocess.Popen([sys.executable, "-c", CHILD, db],
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE, text=True)
    stdout, stderr = child.communicate(timeout=20)
    assert child.returncode == 0, stderr
    runs = json.loads(stdout)
    sf = SkillFlow(db)
    for rid in runs:
        owner = sf._conn.execute("SELECT claimed_by FROM skillflow_steps WHERE run_id=?", (rid,)).fetchone()[0]
        assert owner_is_dead(owner) is True, "the positively exited child must own the claim"
    return sf, runs


def snapshot(sf, rid):
    """Every run/claim/trace row and any event mentioning this run."""
    result = {}
    for table in ("skillflow_runs", "skillflow_steps", "skillflow_trace"):
        columns = [row[1] for row in sf._conn.execute(f"PRAGMA table_info({table})")]
        key = "id" if table == "skillflow_runs" else "run_id"
        result[table] = [dict(row) for row in sf._conn.execute(
            f"SELECT * FROM {table} WHERE {key}=? ORDER BY id", (rid,))] if columns else []
    result["events"] = [dict(row) for row in sf._conn.execute(
        "SELECT * FROM skillflow_outbox WHERE instr(payload_json, ?) > 0 ORDER BY id", (rid,))]
    return result


@pytest.mark.parametrize("operation", ["direct", "advance", "reconnect"])
def test_only_selected_dead_run_is_recovered(owned_runs, operation):
    sf, (a, b) = owned_runs
    before_b = snapshot(sf, b)
    before_a = dict(sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=?", (a,)).fetchone())
    if operation == "direct":
        assert sf.recover_stale_claims(run_id=a) == [a]
    elif operation == "advance":
        sf.advance_run(a)
    else:
        reply = RunnerService(sf).next(a)
        assert reply.get("status") != "failed", reply
    assert snapshot(sf, b) == before_b
    after_a = dict(sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=?", (a,)).fetchone())
    assert after_a["version"] > before_a["version"]
    assert after_a["retry_count"] == before_a["retry_count"]
    assert after_a["release_count"] == before_a["release_count"]
    if operation != "reconnect":
        assert after_a["claim_epoch"] == before_a["claim_epoch"]
    else:
        assert after_a["claim_epoch"] == before_a["claim_epoch"] + 1
    assert sf.recover_stale_claims() == [b]
    assert sf._conn.execute("SELECT status FROM skillflow_steps WHERE run_id=?", (b,)).fetchone()[0] == "pending"


@pytest.mark.parametrize("bad", ["", " ", " padded ", "bad\x00id", 0, False, [], {}])
def test_invalid_scope_refuses_before_any_effect(owned_runs, bad):
    sf, runs = owned_runs
    before = "\n".join(sf._conn.iterdump())
    with pytest.raises((SkillFlowError, ValueError, TypeError)):
        sf.recover_stale_claims(run_id=bad)
    assert "\n".join(sf._conn.iterdump()) == before


def test_unknown_scope_is_empty(owned_runs):
    sf, runs = owned_runs
    before = "\n".join(sf._conn.iterdump())
    assert sf.recover_stale_claims(run_id="unknown-run") == []
    assert "\n".join(sf._conn.iterdump()) == before


@pytest.mark.parametrize("operation", ["direct", "reconnect"])
def test_live_child_keeps_its_claim(tmp_path, operation):
    db = str(tmp_path / "live.db")
    child = subprocess.Popen([sys.executable, "-c", CHILD, db, "wait"],
                             stdin=subprocess.PIPE, stdout=subprocess.PIPE,
                             stderr=subprocess.PIPE, text=True)
    try:
        runs = json.loads(child.stdout.readline())
        sf = SkillFlow(db)
        a, b = runs
        before = [snapshot(sf, rid) for rid in runs]
        if operation == "direct":
            assert sf.recover_stale_claims(-1, run_id=a) == []
        else:
            reply = RunnerService(sf).next(a)
            assert reply["status"] == "failed" and "busy" in reply["error"]
        assert [snapshot(sf, rid) for rid in runs] == before
    finally:
        child.communicate("done\n", timeout=20)
        assert child.returncode == 0


def test_unknown_owner_keeps_unexpired_lease(owned_runs, monkeypatch):
    sf, (a, b) = owned_runs
    # Preserve real ownership rows; only make observation unavailable, as on a
    # platform where the existing identity provider cannot determine liveness.
    monkeypatch.setattr("skillflow.core.owner_is_dead", lambda owner: None)
    before = [snapshot(sf, rid) for rid in (a, b)]
    assert sf.recover_stale_claims(3600, run_id=a) == []
    reply = RunnerService(sf).next(a)
    assert reply["status"] == "failed" and "busy" in reply["error"]
    assert [snapshot(sf, rid) for rid in (a, b)] == before


def test_same_process_reconnect_keeps_claim_and_proxy_submission(tmp_path):
    from skillflow.graph import PipelineGraph, StepNode, Transition
    sf = SkillFlow(str(tmp_path / "service.db"), workspace_base=str(tmp_path / "ws"),
                   artifact_history=False)
    sf.register_graph(PipelineGraph(name="service", begin="plan", steps=[
        StepNode(id="plan", step_type="agent", checkpoint=True, output_mode="content",
                 output_fixed={"plan": "plan.md"}, transitions=[Transition(to="done")]),
        StepNode(id="done", step_type="agent")]))
    service = RunnerService(sf)
    first = service.start("service", project_id="owned")
    rid = first["run_id"]
    before = snapshot(sf, rid)
    assert service.next(rid)["step"] == "plan"
    assert snapshot(sf, rid) == before
    assert "error" not in service.execute_step_tool(
        rid, "plan", "write_plan", {"content": "retained plan"})
    reply = service.submit(rid, "plan", {})
    assert reply["status"] == "paused", reply
    row = dict(sf._conn.execute("SELECT * FROM skillflow_steps WHERE run_id=?", (rid,)).fetchone())
    original = before["skillflow_steps"][0]
    assert row["claim_epoch"] == original["claim_epoch"]
    assert row["retry_count"] == original["retry_count"]
    assert row["release_count"] == original["release_count"]
    assert json.loads(row["outputs_json"]) == {"plan": "retained plan"}
    assert sf._conn.execute("SELECT COUNT(*) FROM skillflow_outbox WHERE event_type='step_claimed'").fetchone()[0] == 1
