# tests/test_trace_seq_unique.py
# Regression: trace seq must be UNIQUE PER RUN across concurrent writers.
#
# The old implementation cached a per-run counter in-process (seeded once
# from MAX(seq)) — race-free within one SkillFlow instance, but every
# ADDITIONAL instance sharing the DB seeded its own counter and minted
# duplicate seq values (observed live: run de0427cd… had seq 1670, 1668,
# 1664… each twice after its retry/reclaim history). Duplicates break the
# keyset-pagination contract (after_seq skips/dups entries) and crashed a
# downstream UI that keyed on seq.

import threading

import pytest

from skillflow.core import SkillFlow
from skillflow.graph import PipelineGraph, StepNode


def _seqs(sf, run_id):
    return [r[0] for r in sf._conn.execute(
        "SELECT seq FROM skillflow_trace WHERE run_id = ? ORDER BY seq",
        (run_id,)).fetchall()]


def _assert_unique_contiguous(seqs, expected_count):
    assert len(seqs) == expected_count
    assert len(set(seqs)) == expected_count, "duplicate seq values"
    assert seqs == list(range(1, expected_count + 1)), "gaps or non-monotonic seq"


def test_two_instances_sharing_a_db_never_duplicate_seq(tmp_path):
    """The bug: instance B seeded its counter before A's later writes, then
    both counted independently over the same range."""
    db = str(tmp_path / "sf.db")
    a = SkillFlow(db_path=db)
    b = SkillFlow(db_path=db)

    a.trace("run-x", "step", "e1")
    b.trace("run-x", "step", "e2")   # old code: B seeds at MAX=1, writes 2 —
    a.trace("run-x", "step", "e3")   # old code: A's counter also writes 2 (DUP)
    b.trace("run-x", "step", "e4")
    a.trace("run-x", "step", "e5")

    _assert_unique_contiguous(_seqs(a, "run-x"), 5)


def test_reactivation_style_interleaving(tmp_path):
    """A 'reclaimed' run traced by a fresh instance while the original
    instance keeps writing (the retry/reclaim history that produced the
    live duplicates)."""
    db = str(tmp_path / "sf.db")
    original = SkillFlow(db_path=db)
    for i in range(10):
        original.trace("run-r", "step", f"before-{i}")

    reclaimer = SkillFlow(db_path=db)  # fresh process after a reclaim
    for i in range(10):
        reclaimer.trace("run-r", "step", f"reclaim-{i}")
        original.trace("run-r", "step", f"zombie-{i}")  # old writer still alive

    _assert_unique_contiguous(_seqs(original, "run-r"), 30)


def test_threaded_appends_stay_unique(tmp_path):
    db = str(tmp_path / "sf.db")
    sf = SkillFlow(db_path=db)

    def hammer(n):
        for i in range(25):
            sf.trace("run-t", "step", f"t{n}-{i}")

    threads = [threading.Thread(target=hammer, args=(n,)) for n in range(4)]
    for t in threads:
        t.start()
    for t in threads:
        t.join()

    _assert_unique_contiguous(_seqs(sf, "run-t"), 100)


def test_seq_is_per_run(tmp_path):
    db = str(tmp_path / "sf.db")
    sf = SkillFlow(db_path=db)
    sf.trace("run-a", "step", "e")
    sf.trace("run-b", "step", "e")
    sf.trace("run-a", "step", "e")
    assert _seqs(sf, "run-a") == [1, 2]
    assert _seqs(sf, "run-b") == [1]


def test_per_project_trace_db_writes_and_reads(sf_with_trace_db):
    """Trace writes go to per-project trace.db, not the shared DB."""
    sf = sf_with_trace_db

    # Create a run that belongs to a project.
    sf.register_agent_config("mock", model="mock", tools=[])
    graph = _multi_step_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("simple", {"project_id": "proj-abc"})

    # Trace with project_id — should write to per-project DB.
    sf.trace(run_id, "step", "claimed", project_id="proj-abc")
    sf.trace(run_id, "prompt", "user", {"text": "hello"},
             project_id="proj-abc")

    # get_trace resolves the per-project DB automatically.
    traces = sf.get_trace(run_id)
    assert len(traces) == 2
    assert traces[0]["seq"] == 1
    assert traces[0]["category"] == "step"
    assert traces[1]["seq"] == 2
    assert traces[1]["category"] == "prompt"

    # The shared DB should have no trace rows for this run.
    shared_traces = [r[0] for r in sf._conn.execute(
        "SELECT seq FROM skillflow_trace WHERE run_id = ?", (run_id,)
    ).fetchall()]
    assert shared_traces == []


def test_per_project_trace_db_delete_project(sf_with_trace_db, tmp_path):
    """delete_project closes the cached trace connection but doesn't
    touch the shared DB's skillflow_trace table."""
    sf = sf_with_trace_db

    sf.register_agent_config("mock", model="mock", tools=[])
    graph = _multi_step_graph()
    sf.register_graph(graph)
    run_id = sf.create_run("simple", {"project_id": "proj-del"})

    sf.trace(run_id, "step", "claimed", project_id="proj-del")
    assert len(sf.get_trace(run_id)) == 1

    # Delete the project — trace.db still exists on disk (caller
    # handles filesystem cleanup), but the cached connection is closed.
    sf.delete_project("proj-del")
    # Verify cached connection is evicted.
    assert "proj-del" not in sf._trace_conns

    # The trace.db file exists and can be read directly.
    trace_db = tmp_path / "workspaces" / "proj-del" / "trace.db"
    assert trace_db.exists()


def _multi_step_graph():
    from skillflow.graph import PipelineGraph, StepNode
    return PipelineGraph(
        name="simple", begin="a",
        steps=[
            StepNode(id="a", name="Step A", agent_config="mock"),
        ],
    )
