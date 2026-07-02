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
