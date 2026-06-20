"""Regression: inline tool steps must NOT run while advance_run holds the
serialisation lock (`self._lock`).

Root cause of the step-5 infinite loop (2026-06-20): advance_run executed a
tool step (run_tests) *inside* its ``_tx()`` block, which holds ``self._lock``
(an RLock) for the whole block. A slow tool (run_tests at 180s) therefore held
the lock the entire time it ran, so concurrent scheduler ticks blocked, their
claimed steps went stale, were re-claimed, and advance_run re-spawned the tool —
forever (a fresh pytest every few seconds).

The fix executes inline tools OUTSIDE ``_tx()``. This test plants a tool whose
body checks, from a second thread, whether ``self._lock`` is held while it runs:

  * before the fix → another thread cannot take the lock → "locked"
  * after the fix  → another thread takes the lock freely → "free"

Fully deterministic — no sleeps. (A SQLite-write-lock probe does NOT work here:
``trace()`` commits ``self._conn`` mid-tool, which releases the SQLite lock
early; the RLock is the reliable, and more faithful, signal.)

The tool is reached via an agent→native-tool edge, because
``_resolve_next_in_tx`` deliberately leaves current_node=None for that edge, so
the tool is resolved through advance_run's auto-advance loop — the exact path
(5 → 5_test) that held the lock.
"""

import threading

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (
    PipelineGraph,
    StepNode,
    Transition,
    EndCondition,
    EndConditions,
)
from tests.mocks import MockToolLoader


def test_inline_tool_does_not_hold_serialisation_lock(tmp_path):
    db_path = str(tmp_path / "locktest.db")
    observed: list[str] = []

    def lock_probe(**kwargs):
        # Can another thread take skillflow's serialisation lock while this
        # tool runs? If not, advance_run is executing us inside _tx() (holding
        # self._lock) — the bug that blocks concurrent ticks → stale-claim loop.
        held = {}

        def try_acquire():
            got = sf._lock.acquire(blocking=False)
            held["free"] = got
            if got:
                sf._lock.release()

        t = threading.Thread(target=try_acquire)
        t.start()
        t.join()
        observed.append("free" if held.get("free") else "locked")
        return {"passed": True}

    tools = MockToolLoader()
    tools.register("lock_probe", lock_probe)

    sf = SkillFlow(db_path, tool_loader=tools)
    sf.register_agent_config("noop_agent")

    graph = PipelineGraph(
        name="locktest",
        begin="gen",
        steps=[
            StepNode(
                id="gen", step_type="agent", agent_config="noop_agent",
                transitions=[Transition(to="probe")],
            ),
            StepNode(
                id="probe", step_type="tool", tool_name="lock_probe",
                transitions=[Transition(to="done")],
            ),
            StepNode(
                id="done", step_type="agent", agent_config="noop_agent",
                transitions=[],
            ),
        ],
        end_conditions=EndConditions(
            combinator="or",
            conditions=[EndCondition(type="node_reached", node="done",
                                     result="completed")],
        ),
    )
    sf.register_graph(graph)
    run_id = sf.create_run("locktest")
    sf.start_run(run_id)

    # Execute the agent step; its native-tool successor is left to advance_run.
    sf.advance_run(run_id)                       # resolve -> "gen"
    claimed = sf.claim_next_step(run_id)
    assert claimed is not None and claimed.step_id == "gen"
    sf.confirm_step(claimed.token, StepResult(outputs={}, flags={}))

    # advance_run must now resolve gen -> probe (auto-advance) and execute the
    # tool. Bounded so a regression that never runs the tool fails fast.
    for _ in range(5):
        sf.advance_run(run_id)
        if observed:
            break

    assert observed, "lock_probe tool never executed"
    assert all(o == "free" for o in observed), (
        "inline tool executed while advance_run held the serialisation lock "
        f"(self._lock) — blocks concurrent ticks: observed={observed}"
    )
