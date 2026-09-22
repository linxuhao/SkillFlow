"""A round's re-reading must be a number it can read, not one somebody can
reconstruct afterwards.

On 2026-09-21 six rounds died at their turn wall, and how much of the budget
had gone on bytes already served was only knowable by opening each run's own
trace.db and writing SQL against it. That is a cost with no observation point:
nothing in the run, the report or the handoff said it, so nothing could react
to it, and the remedy that was reached for instead was to double the budget.

Two calipers, and they do not agree, so both are reported:

`repaid_reads`      a window overlapping lines this run already got for that
                    file. Paging a file too big for one window in DISJOINT
                    windows is not repaid — it is the only way to see it.
`repeat_path_reads` any read of a file read before. Cruder and higher.

privacy r1 gives 30 and 35. The five between them are five reads that opened a
genuinely new region of a file already touched, and they are named in
`by_file`. 35 is the figure the 2026-09-21 measurement produced, so it is
reported rather than quietly corrected: a calibration that cannot reproduce the
number it is calibrated against is not calibrated.
"""
import sqlite3
from pathlib import Path

import pytest

from skillflow import citations, read_accounting
from skillflow.read_audit import audit
from skillflow.read_tools import unified_read

RUN = "run-accounting"
# The real trace of privacy r1, attempt-8c8461af46e74cef952e99ab1dd50a04,
# run d0cbadd2-11ee-4e8d-9690-df3d6332e6fd — 100/100 turns, no finish_step.
PRIVACY_R1 = Path.home() / ".AItelier/workspaces" / \
    "sg-8c8461af46e74cef952e99ab1dd50a04" / "trace.db"
PRIVACY_R1_TREE = Path.home() / ".AItelier/worktrees" / \
    "d0cbadd2-11ee-4e8d-9690-df3d6332e6fd"


class Ignition:
    def __init__(self):
        self.count = 0

    def bump_if(self, condition):
        if condition:
            self.count += 1
        return condition


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "big.py").write_text(
        "".join(f"line_{n} = {n}\n" for n in range(1, 201)))
    (tmp_path / "src" / "small.py").write_text("a = 1\nb = 2\n")
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def read(root, path, **kw):
    return unified_read(
        {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()},
        path, run_id=RUN, **kw)


# ===========================================================================
# the two poles
# ===========================================================================

def test_reading_only_new_regions_is_never_charged_as_repaid(repo):
    """Paging is not re-reading. If this counted, the number would push a round
    away from the one thing it must do with a file bigger than the window."""
    for start in (0, 50, 100, 150):
        out = read(repo, "src/big.py", start_line=start, end_line=start + 50)
        assert "repaid" not in out
    summary = read_accounting.summary(RUN)
    assert summary["reads"] == 4
    assert summary["distinct_files"] == 1
    assert summary["repaid_reads"] == 0
    assert summary["repaid_lines"] == 0
    assert summary["worst_file"] is None
    # ...while the cruder caliper does charge them, which is why both exist.
    assert summary["repeat_path_reads"] == 3


def test_rereading_one_region_is_counted_and_the_file_is_named(repo):
    for _ in range(4):
        out = read(repo, "src/big.py", start_line=10, end_line=20)
    summary = read_accounting.summary(RUN)
    assert summary["reads"] == 4
    assert summary["repaid_reads"] == 3
    assert summary["worst_file"] == "src/big.py"
    assert summary["by_file"][0]["path"] == "src/big.py"
    assert summary["by_file"][0]["repaid_reads"] == 3
    assert summary["by_file"][0]["repaid_lines"] == 30
    # and the call that spent the turn says so in its own result
    assert out["repaid"] is True
    assert out["lines_already_served"] == 10
    assert out["already_served"] == [[11, 20]]
    assert "src/big.py" in out["repaid_note"]


def test_a_partial_overlap_is_repaid_for_the_overlapping_part_only(repo):
    read(repo, "src/big.py", start_line=0, end_line=20)
    out = read(repo, "src/big.py", start_line=10, end_line=30)
    assert out["repaid"] is True
    assert out["lines_already_served"] == 10   # lines 11..20
    assert read_accounting.summary(RUN)["repaid_lines"] == 10


def test_the_summary_is_reported_for_a_clean_run_too(repo):
    """A number that only appears when a round dies teaches nobody anything
    about the round that nearly died."""
    read(repo, "src/small.py")
    summary = read_accounting.summary(RUN)
    assert summary == {
        "reads": 1, "distinct_files": 1, "repaid_reads": 0,
        "repeat_path_reads": 0, "repaid_lines": 0, "worst_file": None,
        "by_file": [{"path": "src/small.py", "source": "repo", "reads": 1,
                     "repaid_reads": 0, "repaid_lines": 0}]}
    assert read_accounting.one_line(RUN) == \
        "reads=1 files=1 repaid=0 (repeat-path 0)"


def test_an_outline_costs_a_turn_and_can_never_be_a_reread(repo):
    read(repo, "src/big.py", outline=True)
    read(repo, "src/big.py", start_line=0, end_line=50)
    summary = read_accounting.summary(RUN)
    assert summary["reads"] == 2
    assert summary["repaid_reads"] == 0


# ===========================================================================
# calibration against the run the criterion was written from
# ===========================================================================

@pytest.mark.skipif(not PRIVACY_R1.is_file(),
                    reason="privacy r1's trace is not on this host")
def test_the_measure_reproduces_the_privacy_r1_figures():
    """47 / 12 / 35 is what the director counted by hand on 2026-09-21. The
    instrument must produce it from the same trace, or the instrument is
    measuring something else and every later number is unanchored."""
    got = audit(PRIVACY_R1, PRIVACY_R1_TREE if PRIVACY_R1_TREE.is_dir() else None,
                run_id="calibration")
    assert got["reads"] == 47
    assert got["distinct_files"] == 12
    assert got["repeat_path_reads"] == 35
    # The window-aware caliper, on the same reads.
    assert got["repaid_reads"] == 30
    # and no read's window had to be guessed at
    assert got["window_provenance"]["assumed"] == 0
    assert {e["path"] for e in got["by_file"][:2]} == {
        "core/state_commands.py", "core/state_service.py"}
    read_accounting.forget_run("calibration")


@pytest.mark.skipif(not PRIVACY_R1.is_file(),
                    reason="privacy r1's trace is not on this host")
def test_the_two_calipers_differ_by_reads_that_opened_new_ground():
    """The five reads between 30 and 35 are not a rounding error; they are the
    reads a path-only caliper would have told a round to stop making."""
    got = audit(PRIVACY_R1, PRIVACY_R1_TREE if PRIVACY_R1_TREE.is_dir() else None,
                run_id="calibration2")
    assert got["repeat_path_reads"] - got["repaid_reads"] == 5
    read_accounting.forget_run("calibration2")


# ===========================================================================
# mutation: with overlap blinded, the disjoint-paging pole goes wrong
# ===========================================================================

def test_without_the_overlap_test_paging_is_charged_as_rereading(repo, monkeypatch):
    fired = Ignition()
    real = read_accounting._covered

    def path_only(spans, lo, hi):
        # The forbidden caliper: "same path twice" regardless of range.
        fired.bump_if(real(spans, lo, hi) == 0 and bool(spans))
        return 1 if spans else 0

    monkeypatch.setattr(read_accounting, "_covered", path_only)
    for start in (0, 50, 100, 150):
        read(repo, "src/big.py", start_line=start, end_line=start + 50)
    assert fired.count == 3, "the mutation must fire on each disjoint page"
    assert read_accounting.summary(RUN)["repaid_reads"] == 3


def test_the_trace_reader_and_the_live_counter_are_the_same_code(tmp_path):
    """Two implementations would be free to agree with the 2026-09-21 SQL and
    disagree with production. This asserts the offline path calls the live
    ledger rather than owning a copy of the rule."""
    db = tmp_path / "trace.db"
    con = sqlite3.connect(db)
    con.execute("CREATE TABLE skillflow_trace (id INTEGER PRIMARY KEY, "
                "run_id TEXT, step_id TEXT, step_instance_id INTEGER, "
                "seq INTEGER, category TEXT, event TEXT, payload_json TEXT)")
    rows = []
    for seq, (lo, hi) in enumerate([(0, 10), (0, 10), (20, 30)]):
        rows.append((seq * 2, "r", None, None, seq * 2, "tool_call", "read",
                     '{"params": {"path": "x.py", "start_line": %d, '
                     '"end_line": %d}}' % (lo, hi)))
        rows.append((seq * 2 + 1, "r", None, None, seq * 2 + 1, "tool_result",
                     "read", '{"preview": "{}"}'))
    con.executemany("INSERT INTO skillflow_trace VALUES (?,?,?,?,?,?,?,?)", rows)
    con.commit()
    con.close()

    fired = Ignition()
    real = read_accounting.record

    def watched(*args, **kwargs):
        fired.bump_if(True)
        return real(*args, **kwargs)

    import skillflow.read_audit as module
    original = module.read_accounting.record
    module.read_accounting.record = watched
    try:
        got = audit(db, None, run_id="shared")
    finally:
        module.read_accounting.record = original
    assert fired.count == 3
    assert (got["reads"], got["repaid_reads"], got["repeat_path_reads"]) == (3, 1, 2)
    read_accounting.forget_run("shared")
