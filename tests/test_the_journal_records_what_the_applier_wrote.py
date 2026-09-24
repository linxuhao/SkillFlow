"""The edit journal records the positions the applier wrote, in every format.

A citation issued before this run's own edit is translated through the
journal, so the journal must say where that edit actually went. The journal
comes from the applier: each V4A hunk's matched line and its own `-`/`+`
rows, each reference's resolved range. Every edit is recorded as
``(removed [a, b), written length)`` in the offsets of the text with every
line ending in its newline, so replacing or deleting a blank line removes
its newline and is never recorded as a pure insertion.

The node goal (rev 5) states the translation as one table, and nothing else
decides where an older citation lands (`citations.translate`):

- a removed range overlaps the citation: refused;
- a pure insertion strictly inside a non-empty window: refused;
- a pure insertion at a window end: the window follows its own bytes;
- a point (empty range) exactly at an insertion offset: refused, reread;
- anything else: shifted by the edit's offset.

The reviews' cases are replayed below verbatim, each citing the script it
came from:

- coords-r2 review (``~/.AItelier/director/reports/coords-r2-review-20260924/``):
  ``probe4.py`` (sha256 10385541…) H1-H4, ``probe5.py`` (sha256 b50cac6e…)
  H5, H6, H6c;
- coords-r3 review (``~/.AItelier/director/reports/coords-r3-review-20260924/``,
  ``review.md`` sha256 e103617c…): ``probe6.py`` (sha256 115a8600…) T1, T1k,
  T1r, T1v, T2a-e, B1-B5; ``probe8.py`` (sha256 f3d38d4a…) C1, C1b, C1r, C1s,
  C2, C3, C4.

H3's intended bytes (probe4.py) are retracted by name in the goal's rev 5:
its point sits exactly on this run's insertion offset, so it is refused.
"""
from pathlib import Path

import pytest

from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

RUN = "run-the-journal-records-what-the-applier-wrote"
REL = "f.py"


def smap(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def rd(root, s, e, run=RUN):
    """The probes' read: 0-based start, exclusive end."""
    return unified_read(smap(root), REL, start_line=s, end_line=e, run_id=run)


def ap(root, refs=None, patch="", run=RUN):
    return apply_code_patch(patch, root, references=refs, run_id=run)


def v4a(*hunks):
    return ("*** Begin Patch\n*** Update File: f.py\n"
            + "".join("@@\n" + h for h in hunks) + "*** End Patch\n")


def span(root, sha, fl, fc, tl, tc, run=RUN):
    """The probes' span helper: preview the coordinates, return the span sha."""
    got = ap(root, [{"file": REL, "sha": sha, "from_line": fl, "from_col": fc,
                     "to_line": tl, "to_col": tc, "new_text": "?"}], run=run)
    assert got["applied"] is False, got
    return got["spans"][0]["sha"]


@pytest.fixture
def root(tmp_path):
    citations.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)


def put(root, data):
    (root / REL).write_bytes(data)


def own(root, res):
    assert res["applied"] is True, res
    return (root / REL).read_bytes()


def removed(first, last):
    """The refusal for a window over lines this run removed or replaced."""
    return (f"apply_patch preflight: f.py reference 1: lines {first}-{last} "
            "changed since the digest was issued: they were removed or "
            "replaced by an earlier edit in this run; reread the range and "
            "cite the new digest")


def point(line, col):
    """The refusal for a point exactly on this run's insertion offset."""
    return (f"apply_patch preflight: f.py reference 1: span {line}:{col}.."
            f"{line}:{col} is an insertion point and an earlier edit in this "
            "run inserted text exactly there, so which side of that text it "
            "meant cannot be told; reread the range and resend the reference "
            "with the new digest to be shown where it lands now")


def refused(root, res, before, error):
    assert res["applied"] is False, res
    assert res["error"] == error
    assert (root / REL).read_bytes() == before


# ===========================================================================
# coords-r2 review: probe4.py H1-H4, probe5.py H5, H6, H6c
# ===========================================================================

def test_H1_a_span_on_the_shown_line_after_v4a_inserted_an_identical_line_above(root):
    """probe4.py H1."""
    put(root, b"a\nx\nb\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    sx = span(root, w, 2, 0, 2, 1)
    assert own(root, ap(root, patch=v4a(" a\n+x\n x\n b\n"))) == b"a\nx\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": sx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H2_a_sha_only_window_on_the_shown_line_after_the_same_insertion(root):
    """probe4.py H2."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    own(root, ap(root, patch=v4a(" a\n+x\n x\n b\n")))
    res = ap(root, [{"file": REL, "sha": wx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H3_a_point_on_this_run_s_insertion_offset_is_refused(root):
    """probe4.py H3, intended bytes retracted by the goal's rev 5: the point
    2:0 was previewed before blank line 2, and the run's V4A then inserted a
    blank line exactly there. Before or after that line is a guess."""
    put(root, b"a\n\nb\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    ins = span(root, w, 2, 0, 2, 0)
    before = own(root, ap(root, patch=v4a(" a\n+\n \n b\n")))
    assert before == b"a\n\n\nb\n"
    res = ap(root, [{"file": REL, "sha": ins, "new_text": "# note\n"}])
    refused(root, res, before, point(2, 0))


def test_H4_control_the_same_insertion_made_by_a_reference(root):
    """probe4.py H4."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    w1 = rd(root, 0, 1)["citation"]["sha"]
    own(root, ap(root, [{"file": REL, "sha": w1, "new_text": "a\nx"}]))
    res = ap(root, [{"file": REL, "sha": wx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H5_explicit_lines_on_the_shown_line_after_v4a_inserted_an_identical_line_above(root):
    """probe5.py H5."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    assert own(root, ap(root, patch=v4a(" a\n+x\n x\n b\n"))) == b"a\nx\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": wx, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H6_a_window_on_a_line_this_run_s_v4a_deleted_is_refused(root):
    """probe5.py H6. The hunk deletes line 2 and keeps line 3."""
    put(root, b"a\nx\nx\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    before = own(root, ap(root, patch=v4a(" a\n-x\n x\n b\n")))
    assert before == b"a\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    refused(root, res, before, removed(2, 2))


def test_H6c_control_the_same_deletion_by_a_reference_is_refused_for_the_same_reason(root):
    """probe5.py H6c."""
    put(root, b"a\nx\nx\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    w12 = rd(root, 0, 2)["citation"]["sha"]
    before = own(root, ap(root, [{"file": REL, "sha": w12, "from_line": 1,
                                  "to_line": 2, "new_text": "a"}]))
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    refused(root, res, before, removed(2, 2))


def test_a_span_rechecks_its_text_when_the_journal_misdescribes_the_edit(root):
    """The translation trusts the journal. A journal entry that describes a
    different edit from the one made (here written directly, as anything
    other than this run's applier would) must not move a write onto text the
    caller was never shown: the span compares the text it lands on with the
    text it showed. The r1 review's mutant M3 drops that comparison."""
    body = b"import os\n\nDEFAULT_TIMEOUT_SECONDS = 30\n\nDEBUG = False\n"
    put(root, body)
    w = rd(root, 0, 5)["citation"]["sha"]
    shown = span(root, w, 3, 8, 3, 15)
    old = body.decode()[:-1]
    new = old.replace("TIMEOUT", "LIMIT")
    terminated = old + "\n"
    elsewhere = [(len(terminated) - 3, len(terminated) - 1, 0)]
    assert citations.journal_edit(RUN, REL, edits=elsewhere,
                                  sha_before=citations.text_sha(old),
                                  sha_after=citations.text_sha(new)) is True
    put(root, (new + "\n").encode())
    res = ap(root, [{"file": REL, "sha": shown, "new_text": "SPAN"}])
    assert res["applied"] is False, res
    assert "changed since its citation was issued" in res["error"]
    assert (root / REL).read_bytes() == (new + "\n").encode()


# ===========================================================================
# coords-r3 review, probe8.py: windows over a blank line this run replaced or
# deleted (review.md lines 102-120, 224-226), points on a blank first line
# after a top insertion (review.md lines 142-150, 227)
# ===========================================================================

C_SRC = b"def f():\n\n    return 1\n"
C_HUNK = " def f():\n-\n+    x = 1\n     return 1\n"


@pytest.mark.parametrize("case,new_text", [("C1", "def g():\n"), ("C1b", "def g():")])
def test_C1_a_window_over_a_blank_line_this_run_s_v4a_replaced_is_refused(root, case, new_text):
    """probe8.py C1 and C1b. C1b used to write `def g():    x = 1`."""
    put(root, C_SRC)
    w = rd(root, 0, 3)["citation"]["sha"]
    before = own(root, ap(root, patch=v4a(C_HUNK)))
    assert before == b"def f():\n    x = 1\n    return 1\n"
    res = ap(root, [{"file": REL, "sha": w, "from_line": 1, "to_line": 2,
                     "new_text": new_text}])
    refused(root, res, before, removed(1, 2))


def test_C1r_the_same_replacement_made_by_a_reference_is_refused(root):
    """probe8.py C1r."""
    put(root, C_SRC)
    w = rd(root, 0, 3)["citation"]["sha"]
    w2 = rd(root, 1, 2)["citation"]["sha"]
    before = own(root, ap(root, [{"file": REL, "sha": w2, "new_text": "    x = 1"}]))
    assert before == b"def f():\n    x = 1\n    return 1\n"
    res = ap(root, [{"file": REL, "sha": w, "from_line": 1, "to_line": 2,
                     "new_text": "def g():\n"}])
    refused(root, res, before, removed(1, 2))


def test_C1s_a_sha_only_window_over_the_replaced_blank_line_is_refused(root):
    """probe8.py C1s."""
    put(root, C_SRC)
    w12 = rd(root, 0, 2)["citation"]["sha"]
    before = own(root, ap(root, patch=v4a(C_HUNK)))
    res = ap(root, [{"file": REL, "sha": w12, "new_text": "def g():\n"}])
    refused(root, res, before, removed(1, 2))


def test_C2_a_window_across_a_blank_line_this_run_deleted_is_refused(root):
    """probe8.py C2. It used to write `b\\nY\\nc\\n`, eating old line 4."""
    put(root, b"b\n\n\n\nc\n")
    w = rd(root, 0, 5)["citation"]["sha"]
    before = own(root, ap(root, patch=v4a(" b\n \n-\n \n c\n")))
    assert before == b"b\n\n\nc\n"
    res = ap(root, [{"file": REL, "sha": w, "from_line": 2, "to_line": 3,
                     "new_text": "Y"}])
    refused(root, res, before, removed(2, 3))


def test_C3_a_point_on_a_blank_first_line_after_a_top_insertion_is_refused(root):
    """probe8.py C3. It used to fuse `\\nP# header`."""
    put(root, b"\nimport os\n")
    w = rd(root, 0, 2)["citation"]["sha"]
    p = span(root, w, 1, 0, 1, 0)
    before = own(root, ap(root, patch=v4a("+# header\n \n import os\n")))
    assert before == b"# header\n\nimport os\n"
    res = ap(root, [{"file": REL, "sha": p, "new_text": "\nP"}])
    refused(root, res, before, point(1, 0))


def test_C4_the_fuzz7_example_verbatim_is_refused(root):
    """probe8.py C4: ['',''], V4A '+N / blank / blank', point 1:0 with '\\nP'.
    It used to fuse `\\nPN`."""
    put(root, b"\n\n")
    w = rd(root, 0, 2)["citation"]["sha"]
    p = span(root, w, 1, 0, 1, 0)
    before = own(root, ap(root, patch=v4a("+N\n \n \n")))
    assert before == b"N\n\n\n"
    res = ap(root, [{"file": REL, "sha": p, "new_text": "\nP"}])
    refused(root, res, before, point(1, 0))


# ===========================================================================
# coords-r3 review, probe6.py: points on an insertion offset (review.md lines
# 122-160, 210-213), windows after a header insertion (lines 161-168,
# 214-215), one-line blank windows (lines 169, 223)
# ===========================================================================

def test_T1_a_point_before_line_1_after_a_v4a_top_insertion_is_refused(root):
    """probe6.py T1. It used to write `import sys` above the docstring."""
    put(root, b"import os\nx = 1\n")
    w = rd(root, 0, 2)["citation"]["sha"]
    p = span(root, w, 1, 0, 1, 0)
    before = own(root, ap(root, patch=v4a('+"""Doc."""\n import os\n')))
    res = ap(root, [{"file": REL, "sha": p, "new_text": "import sys\n"}])
    refused(root, res, before, point(1, 0))


def test_T1k_the_same_point_one_line_down_is_refused(root):
    """probe6.py T1k. The review's table has this one correct (review.md line
    211); the goal's table refuses a point on an insertion offset wherever it
    is, so this is a false refusal, counted in the delivery note."""
    put(root, b"a = 0\nimport os\nx = 1\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    p = span(root, w, 2, 0, 2, 0)
    before = own(root, ap(root, patch=v4a(' a = 0\n+"""Doc."""\n import os\n')))
    res = ap(root, [{"file": REL, "sha": p, "new_text": "import sys\n"}])
    refused(root, res, before, point(2, 0))


@pytest.mark.parametrize("how", ["reference", "v4a"])
def test_T1r_T1v_one_byte_change_two_formats_one_outcome(root, how):
    """probe6.py T1r (reference, line-start form) and T1v (V4A). The review
    found the same byte change landing in two places (review.md lines
    152-156). Both are now the same point on the same insertion offset."""
    put(root, b"a = 0\nimport os\nx = 1\nz = 2\n")
    w0 = rd(root, 0, 4)["citation"]["sha"]
    p = span(root, w0, 2, 0, 2, 0)
    w4 = rd(root, 3, 4)["citation"]["sha"]
    own(root, ap(root, [{"file": REL, "sha": w4, "new_text": "z = 3"}]))
    if how == "reference":
        w1 = rd(root, 0, 4)["citation"]["sha"]
        p1 = span(root, w1, 2, 0, 2, 0)
        assert p != p1
        own(root, ap(root, [{"file": REL, "sha": p1, "new_text": '"""Doc."""\n'}]))
    else:
        own(root, ap(root, patch=v4a(' a = 0\n+"""Doc."""\n import os\n')))
    before = (root / REL).read_bytes()
    assert before == b'a = 0\n"""Doc."""\nimport os\nx = 1\nz = 3\n'
    res = ap(root, [{"file": REL, "sha": p, "new_text": "import sys\n"}])
    refused(root, res, before, point(2, 0))


T2 = [
    ("T2a", lambda w, w1: [{"file": REL, "sha": w, "from_line": 1, "to_line": 1,
                            "new_text": "import sys"}],
     b"# header\nimport sys\nx = 1\ny = 2\n"),
    ("T2b", lambda w, w1: [{"file": REL, "sha": w, "from_line": 2, "to_line": 2,
                            "new_text": "x = 9"}],
     b"# header\nimport os\nx = 9\ny = 2\n"),
    ("T2c", lambda w, w1: [{"file": REL, "sha": w, "from_line": 1, "to_line": 2,
                            "new_text": "import sys\nx = 9"}],
     b"# header\nimport sys\nx = 9\ny = 2\n"),
    ("T2d", lambda w, w1: [{"file": REL, "sha": w1, "new_text": "import sys"}],
     b"# header\nimport sys\nx = 1\ny = 2\n"),
]


@pytest.mark.parametrize("how", ["v4a", "reference"])
@pytest.mark.parametrize("case,refs_of,intended", T2, ids=[c[0] for c in T2])
def test_T2_a_window_from_line_1_follows_its_bytes_past_a_header_insertion(
        root, case, refs_of, intended, how):
    """probe6.py T2a-d: the header sits on the window's start, which is an
    end of the window, so the window follows its own bytes."""
    put(root, b"import os\nx = 1\ny = 2\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    w1 = rd(root, 0, 1)["citation"]["sha"]
    if how == "v4a":
        own(root, ap(root, patch=v4a("+# header\n import os\n")))
    else:
        wz = rd(root, 0, 3)["citation"]["sha"]
        hp = span(root, wz, 1, 0, 1, 0)
        own(root, ap(root, [{"file": REL, "sha": hp, "new_text": "# header\n"}]))
    res = ap(root, refs_of(w, w1))
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == intended


def test_T2e_a_span_on_line_1_follows_its_bytes_past_a_header_insertion(root):
    """probe6.py T2e."""
    put(root, b"import os\nx = 1\n")
    w = rd(root, 0, 2)["citation"]["sha"]
    s1 = span(root, w, 1, 0, 1, 9)
    own(root, ap(root, patch=v4a("+# header\n import os\n")))
    res = ap(root, [{"file": REL, "sha": s1, "new_text": "import sys"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"# header\nimport sys\nx = 1\n"


B = [
    ("B1", b"a\n\nb\n", " a\n+N\n \n", 2, b"a\nN\nY\nb\n"),
    ("B2", b"a\n\nb\n", " \n+N\n b\n", 2, b"a\nY\nN\nb\n"),
    ("B3", b"a\n\nb\n", "-a\n+A\n \n", 2, b"A\nY\nb\n"),
    ("B4", b"a\n\nb\n", " \n-b\n+B\n", 2, b"a\nY\nB\n"),
    ("B5", b"a\nb\n\nc\n", "+H\n a\n", 3, b"H\na\nb\nY\nc\n"),
]


@pytest.mark.parametrize("case,data,hunk,line,intended", B, ids=[c[0] for c in B])
def test_B_a_one_line_blank_window_beside_this_run_s_edit_writes(
        root, case, data, hunk, line, intended):
    """probe6.py B1-B5. A blank line owns its newline, so it is one character
    wide and an edit beside it meets one of its ends. B2 used to be refused."""
    put(root, data)
    w = rd(root, 0, data.count(b"\n"))["citation"]["sha"]
    own(root, ap(root, patch=v4a(hunk)))
    res = ap(root, [{"file": REL, "sha": w, "from_line": line, "to_line": line,
                     "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == intended


def test_a_window_on_a_blank_line_this_run_s_v4a_deleted_is_refused(root):
    """H6 with the identical lines blank: the deletion removes the blank
    line's newline, which the window owns."""
    put(root, b"a\n\n\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    before = own(root, ap(root, patch=v4a(" a\n-\n \n b\n")))
    assert before == b"a\n\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    refused(root, res, before, removed(2, 2))


def test_a_window_on_a_blank_line_a_reference_filled_is_refused(root):
    """The run replaced the blank line with text: its newline was removed
    and written back after the text, a replacement, not an insertion."""
    put(root, b"a\n\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    w2b = rd(root, 1, 2)["citation"]["sha"]
    before = own(root, ap(root, [{"file": REL, "sha": w2b, "new_text": "filled"}]))
    assert before == b"a\nfilled\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "new_text": "Y"}])
    refused(root, res, before, removed(2, 2))


def test_a_window_on_a_blank_line_away_from_this_run_s_edit_still_writes(root):
    put(root, b"a\n\nb\nc\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    own(root, ap(root, patch=v4a(" a\n \n b\n-c\n+C\n")))
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nY\nb\nC\n"


# ===========================================================================
# the sweep: one edit, three ways to make it, judged by the table alone
# ===========================================================================
#
# Each edit inserts one line or deletes one, over files of repeated and blank
# lines. It is made by V4A, or by a reference in either of the two forms a
# caller writes a whole-line edit in:
#   newline-first  "\n" + text at the end of the line above (insert), or from
#                  the end of the line above to the end of the deleted line
#   line-start     text + "\n" at the start of the line below (insert), or from
#                  the start of the deleted line to the start of the next one
# Older citations are issued before the edit: a one-line window on every line,
# and a point before every line plus one at the end of the last line.
#
# The oracle does not call the engine. Where each old line went follows from
# the edit; a whole-line insertion before old line k sits at the start of that
# line, S[k], in the text with every line ending in its newline (inserting
# "\n" + text before a newline and text + "\n" after it are one change). The
# table then decides: a point at S[k] is refused; a window or point inside a
# deleted line is refused; everything else lands where its line went.
#
# A reference call is only coordinates and text. When two different edits
# send the identical call (on a blank line, "\n" at its end appends a blank
# line after it, and "\n" at its start inserts one before it, and 1:0 is
# both), the oracle expects what both edits expect, and a refusal where they
# disagree: writing either one is a guess.

SHAPES = {
    "one": ["x"],
    "two": ["x", "x"],
    "three": ["x", "x", "x"],
    "blank_between": ["x", "", "x"],
    "two_blanks": ["", ""],
    "run_in_context": ["a", "x", "x", "b"],
}
INSERTED = ("x", "", "N")


def _edits(lines):
    n = len(lines)
    for k in range(n + 1):
        for text in INSERTED:
            yield ("insert", k, text)
    for k in range(n):
        yield ("delete", k, None)


def _formats(lines, edit):
    kind, k, _ = edit
    n = len(lines)
    out = ["v4a"]
    if k > 0:
        out.append("newline-first")
    if (kind == "insert" and k < n) or (kind == "delete" and k < n - 1):
        out.append("line-start")
    return out


CASES = [(shape, edit, fmt) for shape, lines in SHAPES.items()
         for edit in _edits(lines) for fmt in _formats(lines, edit)]
# Per shape of n lines: V4A makes 3 x (n + 1) insertions + n deletions; the
# newline-first form makes 3n insertions + (n - 1) deletions; the line-start
# form the same. That is 12n + 1 cases; n = 1, 2, 3, 3, 2, 4 gives 186.
assert len(CASES) == 186
IDS = [f"{s}-{e[0]}{e[1]}-{e[2]!r}-{f}" for s, e, f in CASES]


def _call(lines, edit, fmt):
    """The reference call: (from_line, from_col, to_line, to_col), new_text."""
    kind, k, text = edit
    if fmt == "newline-first":
        end_above = len(lines[k - 1])
        if kind == "insert":
            return (k, end_above, k, end_above), "\n" + text
        return (k, end_above, k + 1, len(lines[k])), ""
    if kind == "insert":
        return (k + 1, 0, k + 1, 0), text + "\n"
    return (k + 1, 0, k + 2, 0), ""


def _after(lines, edit):
    """The lines after the edit, and where each old line went (None: deleted)."""
    kind, k, text = edit
    if kind == "insert":
        return (lines[:k] + [text] + lines[k:],
                [i if i < k else i + 1 for i in range(len(lines))])
    return (lines[:k] + lines[k + 1:],
            [i if i < k else (None if i == k else i - 1) for i in range(len(lines))])


def _probes(n):
    return [("window", i) for i in range(n)] + [("point", i) for i in range(n + 1)]


def _expected(lines, edit, probe):
    """What the table asks of one older citation after one edit: the lines
    after it, or None for a refusal."""
    kind, k, _ = edit
    n = len(lines)
    after, moved = _after(lines, edit)
    starts = [0]
    for line in lines:
        starts.append(starts[-1] + len(line) + 1)
    what, i = probe
    if what == "window":
        if moved[i] is None:
            return None
        result = list(after)
        result[moved[i]] = "Y"
        return result
    at_offset = starts[i] if i < n else starts[n] - 1
    if kind == "insert" and at_offset == starts[k]:
        return None
    if kind == "delete" and starts[k] <= at_offset < starts[k + 1]:
        return None
    result = list(after)
    result.insert(moved[i] if i < n else moved[n - 1] + 1, "P")
    return result


def _oracle(shape, edit, fmt, probe):
    lines = SHAPES[shape]
    if fmt == "v4a":
        return _expected(lines, edit, probe)
    call = _call(lines, edit, fmt)
    same_call = [e for e in _edits(lines) for f in _formats(lines, e)
                 if f != "v4a" and _call(lines, e, f) == call]
    answers = {repr(_expected(lines, e, probe)) for e in same_call}
    return _expected(lines, edit, probe) if len(answers) == 1 else None


def _outcome(tmp, lines, edit, fmt, probe):
    run = f"sweep-{fmt}-{abs(hash((tuple(lines), edit, probe))):x}"
    citations.forget_run(run)
    root = tmp / run
    root.mkdir()
    (root / REL).write_bytes(("\n".join(lines) + "\n").encode())
    whole = rd(root, 0, len(lines), run=run)["citation"]["sha"]
    what, i = probe
    cite = None
    if what == "point":
        if i < len(lines):
            cite = span(root, whole, i + 1, 0, i + 1, 0, run=run)
        else:
            end = len(lines[-1])
            cite = span(root, whole, i, end, i, end, run=run)
    if fmt == "v4a":
        kind, k, text = edit
        rows = [" " + line for line in lines]
        if kind == "insert":
            rows.insert(k, "+" + text)
        else:
            rows[k] = "-" + lines[k]
        done = ap(root, patch=v4a("\n".join(rows) + "\n"), run=run)
    else:
        (fl, fc, tl, tc), new_text = _call(lines, edit, fmt)
        sha = span(root, whole, fl, fc, tl, tc, run=run)
        done = ap(root, [{"file": REL, "sha": sha, "new_text": new_text}], run=run)
    assert done["applied"] is True, (fmt, lines, edit, done)
    assert _lines(root) == _after(lines, edit)[0]
    if what == "window":
        got = ap(root, [{"file": REL, "sha": whole, "from_line": i + 1,
                         "to_line": i + 1, "new_text": "Y"}], run=run)
    else:
        text = "P\n" if i < len(lines) else "\nP"
        got = ap(root, [{"file": REL, "sha": cite, "new_text": text}], run=run)
    citations.forget_run(run)
    return got["applied"], got.get("error"), _lines(root)


def _lines(root):
    text = (root / REL).read_bytes().decode()
    lines = text.split("\n") if text else []
    if text.endswith("\n"):
        lines.pop()
    return lines


@pytest.mark.parametrize("shape,edit,fmt", CASES, ids=IDS)
def test_an_older_citation_lands_where_the_table_puts_it_or_is_refused(
        tmp_path, shape, edit, fmt):
    lines = SHAPES[shape]
    for probe in _probes(len(lines)):
        expected = _oracle(shape, edit, fmt, probe)
        applied, error, result = _outcome(tmp_path, lines, edit, fmt, probe)
        if expected is None:
            assert applied is False and result == _after(lines, edit)[0], (
                probe, applied, error, result)
        else:
            assert (applied, result) == (True, expected), (probe, error, result)


def _journal(tmp, lines, edit, fmt):
    run = f"journal-{fmt}"
    citations.forget_run(run)
    root = tmp / run
    root.mkdir()
    (root / REL).write_bytes(("\n".join(lines) + "\n").encode())
    whole = rd(root, 0, len(lines), run=run)["citation"]["sha"]
    if fmt == "v4a":
        kind, k, text = edit
        rows = [" " + line for line in lines]
        if kind == "insert":
            rows.insert(k, "+" + text)
        else:
            rows[k] = "-" + lines[k]
        done = ap(root, patch=v4a("\n".join(rows) + "\n"), run=run)
    else:
        (fl, fc, tl, tc), new_text = _call(lines, edit, fmt)
        sha = span(root, whole, fl, fc, tl, tc, run=run)
        done = ap(root, [{"file": REL, "sha": sha, "new_text": new_text}], run=run)
    assert done["applied"] is True, done
    journal = list(citations._JOURNAL[run][REL]["edits"][-1])
    citations.forget_run(run)
    return journal


UNSHARED = [(s, e, f) for s, e, f in CASES if f != "v4a"
            and sum(_call(SHAPES[s], e2, f2) == _call(SHAPES[s], e, f)
                    for e2 in _edits(SHAPES[s]) for f2 in _formats(SHAPES[s], e2)
                    if f2 != "v4a") == 1]
# Of the 108 reference cases, 100 send a call no other edit sends. The other
# 8 are "\n" at 1:0 or 2:0 on a blank line and the deletion 1:0..2:0 of
# `two_blanks`, each sent by two edits, which the sweep above covers.
assert len(UNSHARED) == 100


@pytest.mark.parametrize("shape,edit,fmt", UNSHARED,
                         ids=[f"{s}-{e[0]}{e[1]}-{e[2]!r}-{f}" for s, e, f in UNSHARED])
def test_a_reference_journals_a_whole_line_edit_exactly_as_v4a_does(
        tmp_path, shape, edit, fmt):
    """Both reference forms and V4A record one edit as the same spans, so an
    older citation cannot land differently depending on how the edit was
    written (the T1r / T1v split in review.md lines 152-156)."""
    lines = SHAPES[shape]
    assert _journal(tmp_path, lines, edit, fmt) == _journal(tmp_path, lines, edit, "v4a")
