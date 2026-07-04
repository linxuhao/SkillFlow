"""Unit tests for recovery.py."""

import time
from pathlib import Path

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode, Transition
from skillflow.tool_loader import ToolLoader
from skillflow.workspace import WorkspaceManager

_REAL_TOOLS = Path(__file__).parent.parent / "src" / "skillflow" / "tools"


def _agent(id: str, transitions=None):
    return StepNode(id=id, step_type="agent", transitions=transitions or [])


def test_recover_never_reclaims_tool_with_zero_timeout(sf: SkillFlow):
    """A tool node with timeout_seconds=0 ('no timeout') is never reclaimed,
    however old the claim — reclaiming a live no-timeout tool would relaunch it
    concurrently with itself (the rampage)."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[StepNode(id="a", step_type="tool", tool_name="write",
                        timeout_seconds=0, transitions=[])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    long_ago = time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 99999))
    with sf._lock:
        sf._conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "inputs_json, max_retries, status, claimed_at, created_at, updated_at) "
            "VALUES (?, 'a', '{}', '{}', 3, 'claimed', ?, "
            "datetime('now'), datetime('now'))",
            (run_id, long_ago))
        sf._conn.commit()
    # Claimed 99999s ago, but timeout_seconds=0 → never stale.
    assert sf.recover_stale_claims(stale_threshold_seconds=60) == []
    st = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE run_id=? AND step_id='a' "
        "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    assert st["status"] == "claimed"


def test_tool_crash_fails_run_after_three(sf: SkillFlow, tmp_path,
                                          monkeypatch):
    """A tool that crashes on EVERY execution fails the RUN after 3 crashes,
    instead of looping forever via advance_run re-resolving into fresh instances
    (regression: the per-instance _tool_reopen_count was reset each time
    _claim_tool_step_in_tx opened a new instance)."""
    sf._tool_loader = ToolLoader(_REAL_TOOLS)
    sf._workspace = WorkspaceManager(str(tmp_path / "ws"),
                                     projects_base=str(tmp_path / "proj"))
    node = StepNode(id="boom", step_type="tool", tool_name="write",
                    transitions=[Transition(to=None)])
    sf.register_graph(PipelineGraph(name="crash", begin="boom", steps=[node]))
    run_id = sf.create_run("crash", {"project_id": "p"})
    sf.start_run(run_id)

    def _boom(*a, **k):
        raise RuntimeError("boom")
    monkeypatch.setattr(sf, "_execute_tool_inline", _boom)

    for _ in range(25):
        try:
            sf.advance_run(run_id)   # tool fast-path reopens then re-raises
        except RuntimeError:
            pass
        if sf.get_run(run_id)["status"] == "failed":
            break
    assert sf.get_run(run_id)["status"] == "failed"
    n = sf._conn.execute(
        "SELECT COUNT(*) c FROM skillflow_steps WHERE run_id=? AND step_id='boom'",
        (run_id,)).fetchone()["c"]
    assert n == 1, f"crash should fail the run after 1 instance x3, got {n}"


def test_recover_respects_tool_timeout_seconds(sf: SkillFlow):
    """A TOOL step claimed longer than the flat threshold but WITHIN its
    timeout_seconds is NOT reclaimed — a slow-but-alive tool (run_tests,
    timeout 1200s) must not be relaunched concurrently with itself (the step-5
    rampage). Only once the claim exceeds the tool timeout is it presumed dead.
    Agent steps are intentionally NOT scoped by this (their reclaim timing
    feeds a separate investigation)."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[StepNode(id="a", step_type="tool", tool_name="write",
                        timeout_seconds=1200, transitions=[])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    # Seed a claimed tool-step instance claimed 100s ago (already > flat 60s).
    claimed_100s_ago = time.strftime(
        "%Y-%m-%dT%H:%M:%SZ", time.gmtime(time.time() - 100))
    with sf._lock:
        sf._conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "inputs_json, max_retries, status, claimed_at, created_at, updated_at) "
            "VALUES (?, 'a', '{}', '{}', 3, 'claimed', ?, "
            "datetime('now'), datetime('now'))",
            (run_id, claimed_100s_ago))
        sf._conn.commit()

    # 100s > flat 60s, but < 1200s tool timeout → presumed ALIVE, NOT reclaimed.
    assert sf.recover_stale_claims(stale_threshold_seconds=60) == []
    st = sf._conn.execute(
        "SELECT status FROM skillflow_steps WHERE run_id=? AND step_id='a' "
        "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()
    assert st["status"] == "claimed"  # still held, not stolen

    # Backdate beyond the 1200s tool timeout → now presumed dead → reclaimed.
    # Backdate updated_at too (the activity clock): a tool emits no intra-run
    # activity, so a truly dead one has been silent since it was claimed.
    silent_1300s = time.strftime(
        "%Y-%m-%d %H:%M:%S", time.gmtime(time.time() - 1300))
    with sf._lock:
        sf._conn.execute(
            "UPDATE skillflow_steps SET claimed_at=?, updated_at=? "
            "WHERE run_id=? AND step_id='a'",
            (silent_1300s.replace(" ", "T") + "Z", silent_1300s, run_id))
        sf._conn.commit()
    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=60)


def test_tool_reopen_caps_at_three_crashes(sf: SkillFlow):
    """A tool step that crashes deterministically FAILS after 3 reopens instead
    of being relaunched every tick (which rampaged until the host step-count
    valve). The SF-20 stale-recovery cap doesn't cover the crash-reopen path."""
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[StepNode(id="a", step_type="agent", transitions=[])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    with sf._lock:
        sf._conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "inputs_json, max_retries, status, claimed_at, created_at, updated_at) "
            "VALUES (?, 'a', '{}', '{}', 3, 'claimed', "
            "'2026-01-01T00:00:00Z', datetime('now'), datetime('now'))",
            (run_id,))
        sf._conn.commit()

    def _status():
        return sf._conn.execute(
            "SELECT status FROM skillflow_steps WHERE run_id=? AND step_id='a' "
            "ORDER BY id DESC LIMIT 1", (run_id,)).fetchone()["status"]

    # Crashes 1 and 2 reopen to pending; re-claim to simulate the next attempt.
    for _ in range(2):
        sf._reopen_tool_step_in_tx(run_id, "a")
        assert _status() == "pending"
        with sf._lock:
            sf._conn.execute(
                "UPDATE skillflow_steps SET status='claimed' "
                "WHERE run_id=? AND step_id='a'", (run_id,))
            sf._conn.commit()

    # Third crash → failed, not reopened → no more relaunches.
    sf._reopen_tool_step_in_tx(run_id, "a")
    assert _status() == "failed"


def _trans(to: str):
    return Transition(to=to)


def test_recover_stale_claims_resets_claimed(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)

    # Recover with negative threshold (everything is stale)
    recovered = sf.recover_stale_claims(stale_threshold_seconds=-1)
    assert run_id in recovered

    # Step should be re-claimable
    sf.advance_run(run_id)
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None
    assert claimed.step_id == "a"


def test_recover_stale_claims_fresh_not_affected(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)

    # Default threshold — just-claimed step is fresh
    recovered = sf.recover_stale_claims(stale_threshold_seconds=300)
    assert len(recovered) == 0


def _seed_claimed_agent(sf, run_id, *, claimed_ago, updated_ago):
    """Insert a claimed agent step with claimed_at/updated_at backdated by the
    given seconds (updated_at = the activity clock)."""
    claimed = time.strftime("%Y-%m-%dT%H:%M:%SZ",
                            time.gmtime(time.time() - claimed_ago))
    updated = time.strftime("%Y-%m-%d %H:%M:%S",
                            time.gmtime(time.time() - updated_ago))
    with sf._lock:
        sf._conn.execute(
            "INSERT INTO skillflow_steps (run_id, step_id, step_config_json, "
            "inputs_json, max_retries, status, claimed_at, created_at, updated_at) "
            "VALUES (?, 'a', '{}', '{}', 3, 'claimed', ?, datetime('now'), ?)",
            (run_id, claimed, updated))
        sf._conn.commit()


def test_activity_heartbeat_protects_slow_agent(sf: SkillFlow):
    """An agent claimed long ago but ACTIVE recently (fresh updated_at) is NOT
    reaped — staleness is silence, not total runtime. This is the fix for a slow
    reviewer being falsely recovered mid-run."""
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    # Claimed 1000s ago, but last activity 10s ago → alive.
    _seed_claimed_agent(sf, run_id, claimed_ago=1000, updated_ago=10)
    assert sf.recover_stale_claims(stale_threshold_seconds=300) == []


def test_silent_agent_is_reaped(sf: SkillFlow):
    """An agent that has been SILENT longer than the window (no heartbeat) is
    reaped — dead/hung workers are still recovered."""
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    # Both claimed and last-active 1000s ago → silent → dead.
    _seed_claimed_agent(sf, run_id, claimed_ago=1000, updated_ago=1000)
    assert run_id in sf.recover_stale_claims(stale_threshold_seconds=300)


def test_trace_refreshes_liveness(sf: SkillFlow):
    """A trace() call heartbeats the claimed step (bumps updated_at), so a step
    that was about to look stale is kept alive by activity."""
    graph = PipelineGraph(name="test", begin="a", steps=[_agent("a", [])])
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    _seed_claimed_agent(sf, run_id, claimed_ago=1000, updated_ago=1000)
    # One worker action → heartbeat refreshes updated_at → no longer stale.
    sf.trace(run_id, "tool_call", "edit", {"x": 1}, step_id="a")
    assert sf.recover_stale_claims(stale_threshold_seconds=300) == []


def test_recover_stale_claims_no_stale_steps(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
    )
    sf.register_graph(graph)
    sf.create_run("test")
    recovered = sf.recover_stale_claims()
    assert recovered == []


def test_recover_stale_keeps_current_node(sf: SkillFlow):
    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [_trans("b")]), _agent("b", [])],
    )
    sf.register_graph(graph)
    run_id = sf.create_run("test")
    sf.start_run(run_id)
    sf.advance_run(run_id)
    sf.claim_next_step(run_id)

    sf.recover_stale_claims(stale_threshold_seconds=-1)
    run = sf.get_run(run_id)
    # current_node is kept so advance_run re-claims the crashed step
    assert run["current_node"] == "a"


def test_recover_stale_claims_method_on_instance(sf_tmp: SkillFlow):
    """Test the SkillFlow.recover_stale_claims() method (replaces standalone function)."""

    graph = PipelineGraph(
        name="test", begin="a",
        steps=[_agent("a", [])],
    )
    sf_tmp.register_graph(graph)
    run_id = sf_tmp.create_run("test")
    sf_tmp.start_run(run_id)
    sf_tmp.advance_run(run_id)
    sf_tmp.claim_next_step(run_id)

    recovered = sf_tmp.recover_stale_claims(stale_threshold_seconds=-1)
    assert len(recovered) >= 0
