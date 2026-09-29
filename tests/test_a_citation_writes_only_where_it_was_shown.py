"""A citation writes only at the place it showed the caller when it was issued.

The review of coords-r1 (`~/.AItelier/director/reports/coords-r1-review-20260924/
review.md`, sha256 86f7316f…) found that a digest named its coordinates and
its text but not the version of the file it was issued in. The ledger kept one
record per digest, so the same coordinates previewed again after an edit got
the SAME sha and silently moved the older one: citing it wrote at the new
place and said `applied=True`. Its cases E and S3 (spans) and W and S3w
(windows) are replayed below verbatim from its probes.

Director ruling, 2026-09-24, on what "the file changed after it was issued"
means: changed by anything other than this run's own journaled edits, or the
shown bytes themselves were edited. This run's own edits elsewhere are known
exactly and are translated, not refused. So:

* E, S3, W, S3w — the old citation writes the bytes the review's control
  produced, at the place it was shown, never at the re-issued place;
* T1 — the shown text was edited by this run after the preview: refused;
* T2 — the file was written from outside the run after the citation was
  issued: refused, for a span and for a window.

T1 is the only case the text check alone decides and T2 the only case the
chain check alone decides; they are what kill the review's mutants M3 and M3b.
"""
from pathlib import Path

import pytest

from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

RUN = "run-a-citation-writes-only-where-it-was-shown"
REL = "f.py"
SETTINGS = "app/settings.py"
FIXTURE = Path(__file__).parent / "fixtures" / "eac7cacb_app_settings.py.txt"


def smap(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def rd(root, s, e, rel=REL):
    """The probes' read: 0-based start, exclusive end."""
    return unified_read(smap(root), rel, start_line=s, end_line=e, run_id=RUN)


def ap(root, refs):
    return apply_code_patch("", root, references=refs, run_id=RUN)


@pytest.fixture
def root(tmp_path):
    citations.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)


def put(root, data, rel=REL):
    path = root / rel
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)


# ===========================================================================
# review cases, verbatim: the old citation writes where it was shown
# ===========================================================================

L1_TO_L5 = b"l1\nl2\nl3\nl4\nl5\n"
# probe2_candidate.txt, case E0 (the control, no second preview).
E_SHOWN_PLACE = b"l1\nNEW1\nNEW2\nl2\n# note\nl3\nl4\nl5\n"


def test_review_case_E_an_old_insertion_preview_writes_where_it_was_shown(root):
    """probe2.py case E. An empty range covers '' in every version of the
    file, so before this two previews of 3:0 in two versions were one sha."""
    put(root, L1_TO_L5)
    wa = rd(root, 0, 5)["citation"]["sha"]
    p = ap(root, [{"file": REL, "sha": wa, "from_line": 3, "to_line": 3,
                   "from_col": 0, "to_col": 0, "new_text": "# note\n"}])
    span_a = p["spans"][0]["sha"]
    assert (p["spans"][0]["text"], p["spans"][0]["keeps_after"]) == ("", "l3")
    r = ap(root, [{"file": REL, "sha": wa, "from_line": 1, "to_line": 1,
                   "new_text": "l1\nNEW1\nNEW2"}])
    assert r["applied"] is True
    wb = rd(root, 0, 7)["citation"]["sha"]
    p2 = ap(root, [{"file": REL, "sha": wb, "from_line": 3, "to_line": 3,
                    "from_col": 0, "to_col": 0, "new_text": "# other\n"}])
    span_b = p2["spans"][0]["sha"]
    assert (p2["spans"][0]["text"], p2["spans"][0]["keeps_after"]) == ("", "NEW2")
    assert span_a != span_b

    res = ap(root, [{"file": REL, "sha": span_a, "from_line": 3, "to_line": 3,
                     "from_col": 0, "to_col": 0, "new_text": "# note\n"}])

    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == E_SHOWN_PLACE


def test_review_case_E0_control_writes_the_same_bytes(root):
    """probe2.py case E0: the same flow without the second preview. Its bytes
    are the ones E must produce."""
    put(root, L1_TO_L5)
    wa = rd(root, 0, 5)["citation"]["sha"]
    span_a = ap(root, [{"file": REL, "sha": wa, "from_line": 3, "to_line": 3,
                        "from_col": 0, "to_col": 0,
                        "new_text": "# note\n"}])["spans"][0]["sha"]
    ap(root, [{"file": REL, "sha": wa, "from_line": 1, "to_line": 1,
               "new_text": "l1\nNEW1\nNEW2"}])
    res = ap(root, [{"file": REL, "sha": span_a, "from_line": 3, "to_line": 3,
                     "from_col": 0, "to_col": 0, "new_text": "# note\n"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == E_SHOWN_PLACE


DUP = b"a = 1\nx = 30\nb = 2\nx = 30\nc = 3\n"
# probe.py S3 / S3w and probe2.py W: frame A's line 2 is line 4 after the
# insert, so this is the product that writes where frame A was shown.
DUP_SHOWN_PLACE = b"a = 1\nx = 30\nb = 2\nx = 45\nb = 2\nx = 30\nc = 3\n"


def test_review_case_S3_an_old_span_over_repeated_text_writes_where_it_was_shown(root):
    """probe.py case S3: the same coordinates over the same text `30`,
    previewed in two versions of the file."""
    put(root, DUP, SETTINGS)
    wa = rd(root, 0, 5, SETTINGS)["citation"]["sha"]
    first = ap(root, [{"file": SETTINGS, "sha": wa, "from_line": 2, "to_line": 2,
                       "from_col": 4, "to_col": 6, "new_text": "45"}])
    span_a = first["spans"][0]["sha"]
    ap(root, [{"file": SETTINGS, "sha": wa, "from_line": 1, "to_line": 1,
               "new_text": "a = 1\nx = 30\nb = 2"}])
    wb = rd(root, 0, 7, SETTINGS)["citation"]["sha"]
    second = ap(root, [{"file": SETTINGS, "sha": wb, "from_line": 2, "to_line": 2,
                        "from_col": 4, "to_col": 6, "new_text": "45"}])
    span_b = second["spans"][0]["sha"]
    assert first["spans"][0]["text"] == second["spans"][0]["text"] == "30"
    assert span_a != span_b

    res = ap(root, [{"file": SETTINGS, "sha": span_a, "from_line": 2, "to_line": 2,
                     "from_col": 4, "to_col": 6, "new_text": "45"}])

    assert res["applied"] is True, res
    assert (root / SETTINGS).read_bytes() == DUP_SHOWN_PLACE


def test_review_case_W_an_old_window_writes_where_it_was_read(root):
    """probe2.py case W: the same lines with the same text read in two
    versions of the file. The review measured this one on base as well."""
    put(root, DUP)
    wa = rd(root, 1, 2)["citation"]["sha"]
    w1 = rd(root, 0, 1)["citation"]["sha"]
    r = ap(root, [{"file": REL, "sha": w1, "from_line": 1, "to_line": 1,
                   "new_text": "a = 1\nx = 30\nb = 2"}])
    assert r["applied"] is True
    wb = rd(root, 1, 2)["citation"]["sha"]
    assert wa != wb

    res = ap(root, [{"file": REL, "sha": wa, "from_line": 2, "to_line": 2,
                     "new_text": "x = 45"}])

    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == DUP_SHOWN_PLACE


def test_review_case_S3w_an_old_window_cited_by_sha_alone_writes_where_it_was_read(root):
    """probe.py case S3w: the window form of S3, with the insert made by
    citing the sha alone."""
    put(root, DUP, SETTINGS)
    wa = rd(root, 1, 2, SETTINGS)["citation"]["sha"]
    ap(root, [{"file": SETTINGS, "sha": rd(root, 0, 1, SETTINGS)["citation"]["sha"],
               "new_text": "a = 1\nx = 30\nb = 2"}])
    wb = rd(root, 1, 2, SETTINGS)["citation"]["sha"]
    assert wa != wb

    res = ap(root, [{"file": SETTINGS, "sha": wa, "from_line": 2, "to_line": 2,
                     "new_text": "x = 45"}])

    assert res["applied"] is True, res
    assert (root / SETTINGS).read_bytes() == DUP_SHOWN_PLACE


# ===========================================================================
# every field that decides where a sha resolves is covered by the sha
# ===========================================================================

def test_the_same_range_in_the_same_version_is_one_sha_and_in_another_is_two(root):
    put(root, DUP)
    first = rd(root, 3, 4)["citation"]["sha"]
    assert rd(root, 3, 4)["citation"]["sha"] == first
    ref = {"file": REL, "sha": first, "from_line": 4, "to_line": 4,
           "from_col": 4, "to_col": 6, "new_text": "45"}
    span = ap(root, [ref])["spans"][0]["sha"]
    assert ap(root, [ref])["spans"][0]["sha"] == span

    # This run's own edit BELOW the range: nothing above it moved, the text
    # and offset are the same; only the version differs.
    assert ap(root, [{"file": REL, "sha": rd(root, 4, 5)["citation"]["sha"],
                      "new_text": "c = 4"}])["applied"] is True
    again = rd(root, 3, 4)["citation"]
    assert again["sha"] != first
    assert ap(root, [{**ref, "sha": again["sha"]}])["spans"][0]["sha"] != span


def test_a_span_from_before_an_outside_write_stays_refused_after_the_run_rebuilds_the_same_text(root):
    """The chain is part of the place. Here an outside write intervenes, and
    this run's own edits then rebuild exactly the text and generation the span
    was issued in. Without the chain in the digest and the check, the span
    would resolve through a history it was never issued in."""
    put(root, b"a\nb\nc\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    assert ap(root, [{"file": REL, "sha": w, "from_line": 2, "to_line": 2,
                      "new_text": "B"}])["applied"] is True
    w1 = rd(root, 0, 3)["citation"]["sha"]
    span = ap(root, [{"file": REL, "sha": w1, "from_line": 3, "to_line": 3,
                      "from_col": 0, "to_col": 1, "new_text": "C"}])["spans"][0]
    assert span["text"] == "c"

    put(root, b"a\nb\nc\n")                                  # outside write
    w2 = rd(root, 0, 3)["citation"]["sha"]
    assert ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                      "new_text": "B"}])["applied"] is True
    rebuilt = (root / REL).read_bytes()
    assert rebuilt == b"a\nB\nc\n"

    res = ap(root, [{"file": REL, "sha": span["sha"], "new_text": "C"}])

    assert res["applied"] is False
    assert "reread" in res["error"]
    assert (root / REL).read_bytes() == rebuilt


# ===========================================================================
# T1 / T2: what is refused, and the one check that decides each
# ===========================================================================

BODY = b"import os\n\nDEFAULT_TIMEOUT_SECONDS = 30\n\nDEBUG = False\n"


def test_T1_the_shown_text_edited_after_the_preview_is_refused(root):
    """probe3.py case T1. The inner edit removed text inside the previewed
    span, so the translation refuses it: a removed range overlaps the span.
    The comparison of the text there with the text shown is exercised on its
    own by `test_a_span_rechecks_its_text_when_the_journal_misdescribes_the_edit`
    (tests/test_the_journal_records_what_the_applier_wrote.py)."""
    put(root, BODY)
    w = rd(root, 0, 5)["citation"]["sha"]
    big = ap(root, [{"file": REL, "sha": w, "from_line": 3, "from_col": 0,
                     "to_line": 3, "to_col": 27, "new_text": "X"}])["spans"][0]
    inner = ap(root, [{"file": REL, "sha": w, "from_line": 3, "from_col": 8,
                       "to_line": 3, "to_col": 15, "new_text": "LIMIT"}])["spans"][0]
    assert (big["text"], inner["text"]) == ("DEFAULT_TIMEOUT_SECONDS = 3", "TIMEOUT")
    assert ap(root, [{"file": REL, "sha": inner["sha"],
                      "new_text": "LIMIT"}])["applied"] is True
    edited = (root / REL).read_bytes()
    assert edited == b"import os\n\nDEFAULT_LIMIT_SECONDS = 30\n\nDEBUG = False\n"

    res = ap(root, [{"file": REL, "sha": big["sha"], "new_text": "OTHER = 4"}])

    assert res["applied"] is False
    assert "changed since its citation was issued" in res["error"]
    assert "reread" in res["error"]
    assert (root / REL).read_bytes() == edited


def test_T2_a_span_after_an_outside_write_is_refused(root):
    """probe3.py case T2. Every line reads `x = 30` and the outside write adds
    one more on top, so the text at the span's old offset is unchanged: only
    the chain check can refuse it. Without it the review's mutant M3b wrote
    line 3 instead of the line shown, now line 4."""
    put(root, b"x = 30\n" * 5)
    w = rd(root, 0, 5)["citation"]["sha"]
    span = ap(root, [{"file": REL, "sha": w, "from_line": 3, "from_col": 4,
                      "to_line": 3, "to_col": 6, "new_text": "45"}])["spans"][0]
    put(root, b"x = 30\n" * 6)                               # outside write
    outside = (root / REL).read_bytes()

    res = ap(root, [{"file": REL, "sha": span["sha"], "new_text": "45"}])

    assert res["applied"] is False
    assert "changed since its citation was issued" in res["error"]
    assert "reread" in res["error"]
    assert (root / REL).read_bytes() == outside


def test_T2_a_window_after_an_outside_write_is_refused(root):
    """The window form of T2. Lines 3..3 still read `x = 30`, so the check
    that compares the text at those line numbers passes; before this it wrote
    line 3, which is not the line the read showed."""
    put(root, b"x = 30\n" * 5)
    w = rd(root, 0, 5)["citation"]["sha"]
    put(root, b"x = 30\n" * 6)                               # outside write
    outside = (root / REL).read_bytes()

    res = ap(root, [{"file": REL, "sha": w, "from_line": 3, "to_line": 3,
                     "new_text": "x = 45"}])

    assert res["applied"] is False
    assert "changed since the digest was issued" in res["error"]
    assert "reread" in res["error"]
    assert (root / REL).read_bytes() == outside


# ===========================================================================
# a whole-window replace that changes the line count says so
# ===========================================================================

def test_a_window_replaced_by_fewer_lines_states_the_lines_and_bytes_replaced(root):
    """The review's case S6e: host run eac7cacb's own 10-line window, cited
    by sha alone with a one-line new_text. It writes (the whole window is what
    was cited) and the result says how much it took out."""
    data = FIXTURE.read_bytes()
    put(root, data, SETTINGS)
    cite = rd(root, 1674, 1685, SETTINGS)["citation"]
    assert (cite["start_line"], cite["end_line"]) == (1675, 1684)
    lines = data.decode("utf-8").split("\n")
    window = "\n".join(lines[1674:1684])

    res = ap(root, [{"file": SETTINGS, "sha": cite["sha"],
                     "new_text": "DEFAULT_TIMEOUT_SECONDS = 45"}])

    assert res["applied"] is True, res
    (said,) = res["replaced"]
    assert said["replaced_lines"] == 10
    assert said["replaced_bytes"] == len(window.encode("utf-8"))
    assert said["new_lines"] == 1


def test_the_line_count_is_stated_in_the_file_s_own_bytes(root):
    put(root, b"a\r\nb\r\nc\r\n")
    cite = rd(root, 0, 3)["citation"]
    res = ap(root, [{"file": REL, "sha": cite["sha"], "new_text": "z"}])
    (said,) = res["replaced"]
    assert (said["replaced_lines"], said["replaced_bytes"], said["new_lines"]) == (3, 7, 1)
    assert (root / REL).read_bytes() == b"z\r\n"


def test_a_replace_that_keeps_the_line_count_adds_nothing_to_the_echo(root):
    put(root, BODY)
    cite = rd(root, 2, 3)["citation"]
    res = ap(root, [{"file": REL, "sha": cite["sha"],
                     "new_text": "DEFAULT_TIMEOUT_SECONDS = 45"}])
    assert res["replaced"] == [{"file": REL, "from_line": 3, "to_line": 3,
                                "replaced_chars": 28,
                                "replaced": "DEFAULT_TIMEOUT_SECONDS = 30"}]
