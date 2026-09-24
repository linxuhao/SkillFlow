"""The edit journal records the positions the applier wrote, in every format.

A citation issued before this run's own edit is translated through the
journal, so the journal must say where that edit actually went. A V4A edit
used to be journaled afterwards, from the two texts: their common prefix and
suffix were stripped and the rest recorded as one span. Next to a run of
identical lines that span sits at the far end of the run, not where the hunk
wrote, and older citations were moved onto the wrong copy. The review of
coords-r2 (`~/.AItelier/director/reports/coords-r2-review-20260924/
review.md`, sha256 c98f7341…) measured it; its cases H1-H3 (probe4.py) and
H5, H6 (probe5.py) are replayed below verbatim, with the bytes the node goal
(rev 4) names.

Any reading taken from the two texts has two answers once lines repeat, so
the journal now comes from the applier: each V4A hunk's matched line and its
own `-`/`+` rows. The sweep at the end checks it: for identical-line files,
the same edit made through V4A and through a reference must journal the same
spans and translate every older citation to the same outcome.
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


def v4a(body):
    return "*** Begin Patch\n*** Update File: f.py\n@@\n" + body + "*** End Patch\n"


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


# ===========================================================================
# the review's cases, verbatim
# ===========================================================================

def test_H1_a_span_on_the_shown_line_after_v4a_inserted_an_identical_line_above(root):
    """probe4.py H1."""
    put(root, b"a\nx\nb\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    sx = span(root, w, 2, 0, 2, 1)
    r = ap(root, patch=v4a(" a\n+x\n x\n b\n"))
    assert r["applied"] is True and (root / REL).read_bytes() == b"a\nx\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": sx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H2_a_sha_only_window_on_the_shown_line_after_the_same_insertion(root):
    """probe4.py H2."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    ap(root, patch=v4a(" a\n+x\n x\n b\n"))
    res = ap(root, [{"file": REL, "sha": wx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H3_an_insertion_shown_before_a_blank_line_after_v4a_inserted_a_blank_above(root):
    """probe4.py H3."""
    put(root, b"a\n\nb\n")
    w = rd(root, 0, 3)["citation"]["sha"]
    ins = span(root, w, 2, 0, 2, 0)
    ap(root, patch=v4a(" a\n+\n \n b\n"))
    assert (root / REL).read_bytes() == b"a\n\n\nb\n"
    res = ap(root, [{"file": REL, "sha": ins, "new_text": "# note\n"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\n\n# note\n\nb\n"


def test_H4_control_the_same_insertion_made_by_a_reference(root):
    """probe4.py H4: this was already right, and must stay right."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    w1 = rd(root, 0, 1)["citation"]["sha"]
    ap(root, [{"file": REL, "sha": w1, "new_text": "a\nx"}])
    res = ap(root, [{"file": REL, "sha": wx, "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


def test_H5_explicit_lines_on_the_shown_line_after_v4a_inserted_an_identical_line_above(root):
    """probe5.py H5."""
    put(root, b"a\nx\nb\n")
    wx = rd(root, 1, 2)["citation"]["sha"]
    r = ap(root, patch=v4a(" a\n+x\n x\n b\n"))
    assert r["applied"] is True and (root / REL).read_bytes() == b"a\nx\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": wx, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nx\nY\nb\n"


H6_REFUSAL = ("apply_patch preflight: f.py reference 1: lines 2-2 were themselves "
              "replaced by an earlier edit in this run; reread that range and cite "
              "the new digest")


def test_H6_a_window_on_a_line_this_run_s_v4a_deleted_is_refused(root):
    """probe5.py H6. The hunk deletes line 2 and keeps line 3; before this the
    journal recorded line 3 as the one deleted and wrote `a\\nY\\nb\\n`."""
    put(root, b"a\nx\nx\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    r = ap(root, patch=v4a(" a\n-x\n x\n b\n"))
    assert r["applied"] is True and (root / REL).read_bytes() == b"a\nx\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is False
    assert res["error"] == H6_REFUSAL
    assert (root / REL).read_bytes() == b"a\nx\nb\n"


def test_H6c_control_the_same_deletion_by_a_reference_is_refused_for_the_same_reason(root):
    """probe5.py H6c."""
    put(root, b"a\nx\nx\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    w12 = rd(root, 0, 2)["citation"]["sha"]
    ap(root, [{"file": REL, "sha": w12, "from_line": 1, "to_line": 2, "new_text": "a"}])
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is False
    assert res["error"] == H6_REFUSAL
    assert (root / REL).read_bytes() == b"a\nx\nb\n"


BLANK_REFUSAL = ("apply_patch preflight: f.py reference 1: line 2 is blank and an "
                 "earlier edit in this run covered, began or ended exactly where "
                 "it was, so an empty line now matches it wherever it went; "
                 "reread that range and cite the new digest")


def test_a_window_on_a_blank_line_this_run_s_v4a_deleted_is_refused(root):
    """H6 with the identical lines blank. A blank line's range is zero-width
    and its empty text matches anywhere, so the text check cannot catch the
    move; before this the window wrote `Y` into old line 3."""
    put(root, b"a\n\n\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    r = ap(root, patch=v4a(" a\n-\n \n b\n"))
    assert r["applied"] is True and (root / REL).read_bytes() == b"a\n\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is False
    assert res["error"] == BLANK_REFUSAL
    assert (root / REL).read_bytes() == b"a\n\nb\n"


def test_a_window_on_a_blank_line_a_reference_filled_is_refused(root):
    """The run wrote text into the blank line itself: a pure insertion at its
    offset. Before this the window wrote `Y` in front of that text."""
    put(root, b"a\n\nb\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    w2b = rd(root, 1, 2)["citation"]["sha"]
    ap(root, [{"file": REL, "sha": w2b, "new_text": "filled"}])
    assert (root / REL).read_bytes() == b"a\nfilled\nb\n"
    res = ap(root, [{"file": REL, "sha": w2, "new_text": "Y"}])
    assert res["applied"] is False
    assert res["error"] == BLANK_REFUSAL
    assert (root / REL).read_bytes() == b"a\nfilled\nb\n"


def test_a_window_on_a_blank_line_away_from_this_run_s_edit_still_writes(root):
    put(root, b"a\n\nb\nc\n")
    w2 = rd(root, 1, 2)["citation"]["sha"]
    ap(root, patch=v4a(" a\n \n b\n-c\n+C\n"))
    res = ap(root, [{"file": REL, "sha": w2, "from_line": 2, "to_line": 2,
                     "new_text": "Y"}])
    assert res["applied"] is True, res
    assert (root / REL).read_bytes() == b"a\nY\nb\nC\n"


# ===========================================================================
# the sweep: V4A and reference journal the same edit the same way
# ===========================================================================

SHAPES = {
    "one": ["x"],
    "two": ["x", "x"],
    "three": ["x", "x", "x"],
    "blank_between": ["x", "", "x"],
    "two_blanks": ["", ""],
    "run_in_context": ["a", "x", "x", "b"],
}
INSERTED = ("x", "", "N")


def _edits():
    for shape, lines in SHAPES.items():
        n = len(lines)
        for k in range(n + 1):
            for text in INSERTED:
                yield shape, ("insert", k, text)
        for k in range(n):
            yield shape, ("delete", k, None)


EDITS = list(_edits())
# Sum over the 6 shapes (n = 1, 2, 3, 3, 2, 4 lines) of 3 texts x (n + 1)
# insert positions + n delete positions = 63 + 15 = 78 edits.
assert len(EDITS) == 78


def _after(lines, edit):
    """The lines after the edit, and where each old line went (None: deleted)."""
    kind, k, text = edit
    if kind == "insert":
        return (lines[:k] + [text] + lines[k:],
                [i if i < k else i + 1 for i in range(len(lines))])
    return (lines[:k] + lines[k + 1:],
            [i if i < k else (None if i == k else i - 1) for i in range(len(lines))])


def _v4a_patch(lines, edit):
    kind, k, text = edit
    rows = [" " + line for line in lines]
    if kind == "insert":
        rows.insert(k, "+" + text)
    else:
        rows[k] = "-" + lines[k]
    return v4a("\n".join(rows) + "\n")


def _reference(lines, edit):
    """The same edit through a reference: coordinates, new_text."""
    kind, k, text = edit
    n = len(lines)
    if kind == "insert":
        if k == 0:
            return (1, 0, 1, 0), text + "\n"
        return (k, len(lines[k - 1]), k, len(lines[k - 1])), "\n" + text
    if k < n - 1:
        return (k + 1, 0, k + 2, 0), ""
    if k > 0:
        return (k, len(lines[k - 1]), k + 1, len(lines[k])), ""
    return (1, 0, 1, len(lines[0])), ""


def _make(tmp, lines, edit, fmt, run, probe=None):
    """Fresh file and run; optional probe issued BEFORE the edit; the edit.

    Returns the journal entry the edit left and the probe's citation.
    """
    citations.forget_run(run)
    root = tmp / run
    root.mkdir()
    (root / REL).write_bytes(("\n".join(lines) + "\n").encode())
    whole = rd(root, 0, len(lines), run=run)["citation"]["sha"]
    cite = None
    if probe is not None and probe[0] == "point":
        i = probe[1]
        if i < len(lines):
            cite = span(root, whole, i + 1, 0, i + 1, 0, run=run)
        else:
            end = len(lines[-1])
            cite = span(root, whole, i, end, i, end, run=run)
    if fmt == "v4a":
        done = ap(root, patch=_v4a_patch(lines, edit), run=run)
    else:
        (fl, fc, tl, tc), new_text = _reference(lines, edit)
        sha = span(root, whole, fl, fc, tl, tc, run=run)
        done = ap(root, [{"file": REL, "sha": sha, "new_text": new_text}], run=run)
    assert done["applied"] is True, (fmt, lines, edit, done)
    journal = citations._JOURNAL[run][REL]["edits"][-1]
    return root, whole, cite, list(journal)


def _normalised(root):
    text = (root / REL).read_bytes().decode()
    lines = text.split("\n")
    if text.endswith("\n"):
        lines.pop()
    return lines


def _outcome(tmp, lines, edit, fmt, probe):
    run = f"sweep-{fmt}-{abs(hash((tuple(lines), edit, probe))):x}"
    root, whole, cite, _ = _make(tmp, lines, edit, fmt, run, probe)
    if probe[0] == "window":
        i = probe[1]
        got = ap(root, [{"file": REL, "sha": whole, "from_line": i + 1,
                         "to_line": i + 1, "new_text": "Y"}], run=run)
    else:
        text = "P\n" if probe[1] < len(lines) else "\nP"
        got = ap(root, [{"file": REL, "sha": cite, "new_text": text}], run=run)
    citations.forget_run(run)
    return got["applied"], got.get("error"), _normalised(root)


IDS = [f"{s}-{e[0]}{e[1]}-{e[2]!r}" for s, e in EDITS]


def _probes(n):
    return [("window", i) for i in range(n)] + [("point", i) for i in range(n + 1)]


@pytest.mark.parametrize("shape,edit", EDITS, ids=IDS)
def test_v4a_and_reference_journal_and_translate_the_same_edit_identically(
        tmp_path, shape, edit):
    """Same spans in the journal; every older citation gets the same outcome:
    the same bytes, or refused with the same message."""
    lines = SHAPES[shape]
    _, _, _, j_v4a = _make(tmp_path, lines, edit, "v4a", "journal-v4a")
    _, _, _, j_ref = _make(tmp_path, lines, edit, "reference", "journal-ref")
    assert j_v4a == j_ref, (j_v4a, j_ref)
    for probe in _probes(len(lines)):
        v4a_out = _outcome(tmp_path, lines, edit, "v4a", probe)
        ref_out = _outcome(tmp_path, lines, edit, "reference", probe)
        assert v4a_out == ref_out, (probe, v4a_out, ref_out)


BLANK_WINDOW_REFUSAL = ("line {n} is blank and an earlier edit in this run "
                        "covered, began or ended exactly where it was")


@pytest.mark.parametrize("shape,edit", EDITS, ids=IDS)
def test_an_older_citation_lands_where_its_line_went_or_is_refused(
        tmp_path, shape, edit):
    """Where each old line went is known from the edit itself: a window on it
    writes there, or is refused when the edit deleted it. An insertion point
    before an old line stays before it. A blank line may also be refused."""
    lines = SHAPES[shape]
    n = len(lines)
    after, moved = _after(lines, edit)
    kind, k, _ = edit
    for probe in _probes(n):
        applied, error, result = _outcome(tmp_path, lines, edit, "v4a", probe)
        i = probe[1]
        if probe[0] == "window":
            if moved[i] is None:
                assert applied is False, (probe, applied, error, result)
            elif lines[i] == "" and applied is False:
                # A blank line is a zero-width range; next to an edit its
                # empty text would match on either side, so it is refused
                # (a false refusal when the line itself survived, listed in
                # the delivery note).
                assert BLANK_WINDOW_REFUSAL.format(n=i + 1) in error, error
            elif kind == "insert" and k == 0 and i == 0:
                # A top insertion is journaled at offset 0, where remap keeps
                # an offset in place, so the window on old line 1 now spans
                # the inserted text and its own and no longer matches: a
                # false refusal, listed in the delivery note.
                assert applied is False, (probe, applied, error, result)
            else:
                expected = list(after)
                expected[moved[i]] = "Y"
                assert (applied, result) == (True, expected), (probe, error)
        elif kind == "insert":
            expected = list(after)
            if i < n:
                # A point at the top stays at offset 0, before a top insertion.
                at = 0 if (k == 0 and i == 0) else moved[i]
            else:
                at = moved[n - 1] + 1
            expected.insert(at, "P")
            assert (applied, result) == (True, expected), (probe, error)
