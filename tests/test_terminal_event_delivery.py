"""A run that ends must SAY so — on the bus and in the outbox.

Live, 2026-09-05 post-activation smoke: two `coding_impl` runs reached
`status=completed` and no `run_completed` notification was ever published.
`debugctl await --run … --follow`, the documented driver wait path, therefore
waited out its full 900 s on a run that had finished 15 minutes earlier. The
run row, `get_run_summary` and the tick log all said `completed`; only the push
was missing, so every consumer that waits instead of polling was blind.

The cause is not in the completion path. `_complete_tool_step` reaches
`_complete_run_in_tx`, which publishes, and in a single-threaded test it
arrives. It is in how the outbox is written:

    NotificationBus.publish()  ->  self._write_outbox(notification)

`_write_outbox` INSERTs and commits on SkillFlow's SHARED sqlite connection
while holding NO lock. The host runs `advance_run` in a worker thread
(AItelier core/scheduler.py `_advance_off_the_loop` -> `asyncio.to_thread`) and
publishes bridge back to the event loop via `call_soon_threadsafe`, so the
outbox INSERT executes on the LOOP thread while a WORKER thread is inside
`SkillFlow._tx()` with `BEGIN IMMEDIATE` open. Two threads, one connection, two
different locks — `SkillFlow._lock` for transactions, `NotificationBus._conn_lock`
for one of the two outbox paths and nothing at all for the other.

The damage is silent twice over: sqlite raises
`cannot start a transaction within a transaction`, `_write_outbox` swallows it
with a bare `except Exception: pass`, and the event is gone with no trace.

These tests use ordinary threads, a temporary SQLite file and the public API.
No fault injection, no child process, no patched internals.
"""

import asyncio
import sqlite3
import threading

import pytest

from skillflow.core import SkillFlow, StepResult
from skillflow.graph import (EndCondition, EndConditions, PipelineGraph,
                             StepNode, Transition)
from tests.mocks import MockToolLoader


def _graph(mid_is_tool: bool) -> PipelineGraph:
    mid = (StepNode(id="mid", step_type="tool", tool_name="noop_tool",
                    transitions=[Transition(to="done")])
           if mid_is_tool else
           StepNode(id="mid", step_type="agent", agent_config="noop_agent",
                    transitions=[Transition(to="done")]))
    return PipelineGraph(
        name="g", begin="gen",
        steps=[StepNode(id="gen", step_type="agent", agent_config="noop_agent",
                        transitions=[Transition(to="mid")]),
               mid,
               StepNode(id="done", step_type="agent", agent_config="noop_agent",
                        transitions=[])],
        end_conditions=EndConditions(combinator="or", conditions=[
            EndCondition(type="node_reached", node="done",
                         result="completed")]))


def _engine(tmp_path, name):
    tools = MockToolLoader()
    tools.register("noop_tool", lambda **k: {"passed": True})
    sf = SkillFlow(str(tmp_path / f"{name}.db"), tool_loader=tools)
    sf.register_agent_config("noop_agent")
    return sf


def _outbox(sf, event_type):
    return [dict(r) for r in sf._conn.execute(
        "SELECT event_type FROM skillflow_outbox WHERE event_type = ?",
        (event_type,))]


# ── the host's threading model, end to end ───────────────────────────

async def _drive_like_the_host(sf, run_id, mid_is_tool):
    """`advance_run` on a worker thread, exactly as the host runs it."""
    async def advance():
        return await asyncio.to_thread(sf.advance_run, run_id)

    await advance()
    sf.confirm_step(sf.claim_next_step(run_id).token, StepResult())
    for _ in range(8):
        await advance()
        if sf.get_run(run_id)["status"] in ("completed", "failed"):
            break
        claimed = sf.claim_next_step(run_id)
        if claimed is not None:
            sf.confirm_step(claimed.token, StepResult())
    await asyncio.sleep(0.25)          # let the bridged publishes land


@pytest.mark.parametrize("mid_is_tool", [False, True],
                         ids=["agent_step_reaches_done", "tool_step_reaches_done"])
async def test_a_finished_run_publishes_run_completed(tmp_path, mid_is_tool):
    """THE test. The tool case is the one the smoke observed; the agent case is
    the control, and both must deliver."""
    seen: list[str] = []

    async def subscriber(n):
        seen.append(n.event_type)

    sf = _engine(tmp_path, f"e2e-{mid_is_tool}")
    sf.notifications.subscribe(subscriber)
    sf.notifications.set_event_loop(asyncio.get_running_loop())
    sf.register_graph(_graph(mid_is_tool))
    run_id = sf.create_run("g")
    sf.start_run(run_id)

    await _drive_like_the_host(sf, run_id, mid_is_tool)

    assert sf.get_run(run_id)["status"] == "completed"
    assert "run_completed" in seen, (
        f"the run finished and no subscriber was told: {seen}")
    assert _outbox(sf, "run_completed"), (
        "the run finished and the outbox has no terminal event — a consumer "
        "that waits instead of polling can never learn the run ended")


async def test_the_terminal_event_is_published_exactly_once(tmp_path):
    """A fix that delivers by publishing twice is not a fix."""
    seen: list[str] = []

    async def subscriber(n):
        seen.append(n.event_type)

    sf = _engine(tmp_path, "once")
    sf.notifications.subscribe(subscriber)
    sf.notifications.set_event_loop(asyncio.get_running_loop())
    sf.register_graph(_graph(True))
    run_id = sf.create_run("g")
    sf.start_run(run_id)

    await _drive_like_the_host(sf, run_id, True)

    assert seen.count("run_completed") == 1, seen
    assert len(_outbox(sf, "run_completed")) == 1


# ── the mechanism, deterministically ─────────────────────────────────

def test_an_outbox_write_cannot_commit_another_threads_transaction(tmp_path):
    """The mechanism, pinned by observation rather than by luck.

    A worker thread opens a `SkillFlow._tx()` and writes a sentinel row inside
    it. A second thread then writes to the outbox on the SAME connection, which
    is what the event loop does when a worker-thread publish is bridged back.

    While the worker's transaction is still open, a THIRD, independent
    connection must not be able to see the sentinel — an uncommitted row is
    nobody else's business. Pre-fix the outbox write's `commit()` ends the
    worker's transaction from the wrong thread and the sentinel becomes visible
    early; that early commit is the same defect that leaves an implicit
    transaction open and makes the next `BEGIN IMMEDIATE` raise
    `cannot start a transaction within a transaction`.
    """
    sf = _engine(tmp_path, "race")
    from skillflow.notifications import Notification

    db_path = str(tmp_path / "race.db")
    inside = threading.Event()
    wrote = threading.Event()
    may_finish = threading.Event()
    errors: list = []
    visible_early: list = []

    def worker():
        try:
            with sf._tx() as conn:
                conn.execute(
                    "INSERT INTO skillflow_runs (id, graph_name, status) "
                    "VALUES ('sentinel', 'g', 'running')")
                inside.set()
                assert may_finish.wait(10)
        except Exception as e:                              # noqa: BLE001
            errors.append(e)

    def outbox_writer():
        assert inside.wait(10)
        sf.notifications._write_outbox(
            Notification(event_type="run_completed",
                         payload={"run_id": "r1", "reason": "Node 'done' reached"},
                         run_id="r1"))
        wrote.set()

    w = threading.Thread(target=worker, daemon=True)
    o = threading.Thread(target=outbox_writer, daemon=True)
    w.start(); o.start()
    assert inside.wait(10), "the worker never entered its transaction"

    # Give the outbox writer a real chance to do damage, then look from outside.
    wrote.wait(1.0)
    probe = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        seen = probe.execute(
            "SELECT COUNT(*) FROM skillflow_runs WHERE id = 'sentinel'").fetchone()[0]
    except sqlite3.OperationalError:
        seen = 0                       # locked is also "not visible"
    finally:
        probe.close()
    if seen:
        visible_early.append(seen)

    may_finish.set()
    w.join(10); o.join(10)
    assert not w.is_alive() and not o.is_alive()
    assert not errors, f"the open transaction was corrupted: {errors}"
    assert not visible_early, (
        "an outbox write committed another thread's open transaction")

    # The connection must still be usable — the line that fails first when an
    # implicit transaction was left behind.
    with sf._tx() as conn:
        conn.execute("SELECT 1")
    assert _outbox(sf, "run_completed"), "the outbox write was lost"


def test_a_lost_outbox_write_is_not_silent(tmp_path, caplog):
    """`except Exception: pass` is how this hid for months. A dropped event must
    leave a record even when it must not raise."""
    import logging
    sf = _engine(tmp_path, "loud")
    from skillflow.notifications import Notification

    class Broken:
        def execute(self, *a, **k):
            raise sqlite3.OperationalError("cannot start a transaction within a transaction")

        def commit(self):
            pass

    sf.notifications._conn = Broken()
    with caplog.at_level(logging.WARNING):
        sf.notifications._write_outbox(
            Notification(event_type="run_completed", payload={}, run_id="r1"))
    assert any("run_completed" in r.getMessage() for r in caplog.records), (
        "an outbox write was dropped without a word in the log")
