"""A successful write must not cost the caller a reread before the next one.

Measured, `growth.proficiency` r9 (attempt-ddc1377c23fd4923a8837149538c9cd1,
run 19f3f2c5, 200/200 turns, never reached finish_step): one file read 54 times
and written 37 times, ratio 1.46; 66 reads over 10 files of which 54 re-served
lines the run already had; 79 apply_patch calls with 20 refusals, 19 of them
coordinate-class and 14 of those the literal shape

    apply_patch preflight: tests/growth_number_scan.py: to column 78 is past
    the end of line 746 (0 characters); reread the range

The caller was quoting the columns it had read. Its own earlier write had
moved them. The engine told it to read again, and it did, 54 times.

The second half of the same defect is worse and is not a refusal at all.
privacy r2 (attempt-00bbbb91b61e46b184e228c04e94b195, 204/204) cited a window
it had read one turn earlier and asked for line 103 columns 17..40, meaning the
whole 58-character line. Columns 17..40 were inside the line, so every check
passed and `applied` came back true — having replaced `if action in _covered_a`
in the middle of an expression. The file was left with a six-line comment
spliced into the expression and `]ctions() else []),` orphaned below it, which
is a SyntaxError at collection: that round scored no tests at all.

So there are three claims here, and each has its opposite pole:
  1. coordinates from before a write still resolve after it;
  2. a range that was itself replaced is REFUSED, by name, not guessed at;
  3. the caller never has to count columns for a whole-line edit, and what was
     actually replaced comes back in the result rather than having to be
     discovered by reading the file again.
"""
import pytest

from skillflow import citations, read_accounting
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

RUN = "run-frozen-frame"
BODY = "\n".join([
    "def answer():",            # 1
    "    first = 1",            # 2
    "    second = 2",           # 3
    "    third = 3",            # 4
    "    fourth = 4",           # 5
    "    return first",         # 6
    "",                         # 7
    "def helper():",            # 8
    "    return 0",             # 9
]) + "\n"


class Ignition:
    """A mutation that never fires and a defect that is never caught leave the
    same green log. This counts the firings, and a mutation test asserts the
    count is above zero before it believes its own result."""

    def __init__(self):
        self.count = 0

    def bump_if(self, condition):
        if condition:
            self.count += 1
        return condition


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def read(root, path="src/a.py", run_id=RUN, **kw):
    return unified_read(smap_for(root), path, run_id=run_id, **kw)


def apply(root, references=None, patch="", run_id=RUN):
    return apply_code_patch(patch, root, references=references, run_id=run_id)


def apply_corroborated(root, references, run_id=RUN):
    """Name columns, be shown what they cover, then cite that span.

    Columns are refused until they cite a span citation issued for exactly
    those coordinates; the refusal writes nothing and carries the spans.
    Returns the preview too, so a test can assert what the caller was shown.
    """
    before = (root / "src" / "a.py").read_bytes()
    preview = apply(root, references, run_id=run_id)
    assert preview["applied"] is False, preview
    assert preview["written"] == [] and preview["partial"] is False
    assert (root / "src" / "a.py").read_bytes() == before
    spans = {span["reference"]: span for span in preview["spans"]}
    cited = [{**body, "sha": spans[i]["sha"]} if i in spans else body
             for i, body in enumerate(references, 1)]
    return apply(root, cited, run_id=run_id), preview


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def ref(cite, from_line, to_line, new_text, from_col=None, to_col=None):
    body = {"file": "src/a.py", "sha": cite["sha"], "from_line": from_line,
            "to_line": to_line, "new_text": new_text}
    if from_col is not None:
        body["from_col"] = from_col
    if to_col is not None:
        body["to_col"] = to_col
    return body


# ===========================================================================
# 1. the coordinates a read issued survive this run's own writes
# ===========================================================================

def test_a_second_edit_below_the_first_needs_no_read_in_between(repo):
    """The shape r9 died of: edit, then edit lower down, same file."""
    cite = read(repo)["citation"]
    reads_after_the_only_read = read_accounting.summary(RUN)["reads"]

    first = apply(repo, [ref(cite, 2, 2, "    first = 'grew by a lot'")])
    assert first["applied"] is True, first

    # No read here. That is the whole test. The line lengths and the line
    # numbering below line 2 have both moved.
    second = apply(repo, [ref(cite, 5, 5, "    fourth = 44")])
    assert second["applied"] is True, second

    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    first = 1", "    first = 'grew by a lot'").replace(
        "    fourth = 4", "    fourth = 44")
    # One read served this whole exchange.
    assert read_accounting.summary(RUN)["reads"] == reads_after_the_only_read == 1
    assert read_accounting.summary(RUN)["repaid_reads"] == 0


def test_a_second_edit_above_the_first_needs_no_read_in_between(repo):
    """The opposite direction, so that 'edit from the bottom upwards' — a
    discipline pushed onto every future caller, which no measurement would
    ever report as broken — cannot pass for a fix."""
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 5, 5, "    fourth = 'four' * 40")])["applied"]
    second = apply(repo, [ref(cite, 2, 2, "    first = 11")])
    assert second["applied"] is True, second
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    fourth = 4", "    fourth = 'four' * 40").replace(
        "    first = 1", "    first = 11")
    assert read_accounting.summary(RUN)["reads"] == 1


def test_many_writes_in_a_row_from_one_read(repo):
    """r9's ratio was 54 reads to 37 writes. One read, four writes, and the
    fourth is addressed in the coordinates the first read reported."""
    cite = read(repo)["citation"]
    for line, text in ((2, "    first = 1111"), (3, "    second = 2222"),
                       (4, "    third = 3333"), (5, "    fourth = 4444")):
        got = apply(repo, [ref(cite, line, line, text)])
        assert got["applied"] is True, (line, got)
    assert (repo / "src" / "a.py").read_text() == (
        "def answer():\n    first = 1111\n    second = 2222\n"
        "    third = 3333\n    fourth = 4444\n    return first\n\n"
        "def helper():\n    return 0\n")
    assert read_accounting.summary(RUN)["reads"] == 1


def test_insertions_that_change_the_line_count_still_leave_the_rest_addressable(repo):
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 2, 2,
                            "    first = 1\n    inserted_a = 0\n"
                            "    inserted_b = 0")])["applied"]
    later = apply(repo, [ref(cite, 9, 9, "    return 99")])
    assert later["applied"] is True, later
    assert (repo / "src" / "a.py").read_text().endswith(
        "def helper():\n    return 99\n")


# ===========================================================================
# 2. the opposite pole: a range that really is gone is refused, by name
# ===========================================================================

def test_citing_a_range_this_run_itself_replaced_is_refused(repo):
    """The same whole line, twice. Its ends still translate — they are the
    edit's own boundaries — so this is caught by the range no longer reading
    the same, which is the honest reason and says to reread."""
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 3, 3, "    second = 'replaced'")])["applied"]
    got = apply(repo, [ref(cite, 3, 3, "    second = 'again'")])
    assert got["applied"] is False
    assert "src/a.py" in got["error"] and "3-3" in got["error"]
    assert "changed since the digest was issued" in got["error"]
    assert "reread the range" in got["error"]
    assert got["written"] == [] and got["partial"] is False
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    second = 2", "    second = 'replaced'")


def test_citing_inside_a_span_this_run_replaced_is_refused_by_name(repo):
    """Strictly inside, so there is no honest translation at all: that text is
    gone and nobody has read what took its place. A different refusal from the
    one above, and it must not be allowed to become a guess."""
    cite = read(repo)["citation"]
    assert apply_corroborated(
        repo, [ref(cite, 3, 3, "SECOND", from_col=4, to_col=10)])[0]["applied"]
    got = apply(repo, [ref(cite, 3, 3, "x", from_col=5, to_col=8)])
    assert got["applied"] is False
    assert "src/a.py" in got["error"] and "3-3" in got["error"]
    assert "replaced by an earlier edit in this run" in got["error"]
    assert got["written"] == [] and got["partial"] is False


def test_a_column_that_was_never_in_the_line_is_refused_naming_it(repo):
    cite = read(repo, start_line=1, end_line=2)["citation"]
    got = apply(repo, [ref(cite, 2, 2, "x", from_col=0, to_col=500)])
    assert got["applied"] is False
    assert "src/a.py" in got["error"] and "line 2" in got["error"]
    assert "as the read served it" in got["error"]
    assert (repo / "src" / "a.py").read_text() == BODY


def test_a_write_from_outside_the_engine_still_invalidates_the_citation(repo):
    """The frame is trusted only while the file is what the engine left there.
    Without this the journal would be translating coordinates through edits it
    knows nothing about, which is worse than requiring a reread."""
    cite = read(repo)["citation"]
    (repo / "src" / "a.py").write_text(BODY.replace("    third = 3",
                                                    "    third = 3  # by hand"))
    changed = (repo / "src" / "a.py").read_text()
    got = apply(repo, [ref(cite, 2, 2, "    first = 11")])
    assert got["applied"] is False
    assert "changed since the digest was issued" in got["error"]
    assert (repo / "src" / "a.py").read_text() == changed


def test_a_sha_this_run_never_issued_is_still_refused(repo):
    read(repo)
    got = apply(repo, [{"file": "src/a.py", "sha": "0" * 40, "from_line": 2,
                        "to_line": 2, "new_text": "x"}])
    assert got["applied"] is False and "never issued" in got["error"]


# ===========================================================================
# 3. whole-line edits need no column arithmetic, and what went is reported
# ===========================================================================

def test_omitting_the_columns_replaces_exactly_those_whole_lines(repo):
    cite = read(repo)["citation"]
    got = apply(repo, [ref(cite, 3, 4, "    merged = 23")])
    assert got["applied"] is True, got
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    second = 2\n    third = 3\n", "    merged = 23\n")


def test_the_result_says_what_it_replaced(repo):
    """privacy r2's corruption was legal, silent and only visible by reading
    the file back. The span comes back in the result now."""
    cite = read(repo)["citation"]
    got, _ = apply_corroborated(repo, [ref(cite, 2, 2, "    first = 1  # noted",
                                           from_col=4, to_col=9)])
    assert got["applied"] is True
    assert got["replaced"] == [{"file": "src/a.py", "from_line": 2,
                                "to_line": 2, "replaced_chars": 5,
                                "replaced": "first"}]


def test_the_privacy_r2_corruption_shape(repo):
    """Replayed from the real reference hunk at seq 1767 of
    attempt-00bbbb91b61e46b184e228c04e94b195: a fresh, valid citation, a column
    range inside the line, and the wrong 23 characters replaced.

    The columns still do exactly what they say — an editor that second-guessed
    them would be unusable. What changed is that the caller no longer has to
    supply them for a whole-line edit, that the result now names what went,
    and that a column is never written before the caller has been shown the
    exact text it covers and has cited the span the engine issued for it.
    """
    line = "                 if action in _covered_actions() else []),\n"
    (repo / "src" / "a.py").write_text(line)
    citations.forget_run(RUN)
    cite = read(repo)["citation"]
    assert len(line.rstrip("\n")) == 58

    wrong, shown = apply_corroborated(
        repo, [ref(cite, 1, 1, "NEW", from_col=17, to_col=40)])
    # Before anything is written, the caller is shown the 23 characters.
    assert shown["spans"][0]["text"] == "if action in _covered_a"
    assert wrong["applied"] is True
    # It is visible now, in the result, without reading the file again.
    assert wrong["replaced"][0]["replaced"] == "if action in _covered_a"
    assert wrong["replaced"][0]["replaced_chars"] == 23

    # And the way to mean "this whole line" carries no columns at all.
    (repo / "src" / "a.py").write_text(line)
    citations.forget_run(RUN)
    cite = read(repo)["citation"]
    right = apply(repo, [ref(cite, 1, 1, "WHOLE")])
    assert right["applied"] is True
    assert right["replaced"][0]["replaced"] == line.rstrip("\n")
    assert (repo / "src" / "a.py").read_text() == "WHOLE\n"


# ===========================================================================
# mutation: with the frame removed, the second write DOES need a reread
# ===========================================================================

def test_without_the_frozen_frame_the_second_write_fails(repo, monkeypatch):
    """The claim is 'no read is needed in between'. That is only worth
    anything if a read IS needed without this mechanism — otherwise the green
    above could be measuring nothing at all. The counter proves the branch
    being removed was genuinely being taken."""
    fired = Ignition()
    real = citations.chain_intact

    def disabled(*args, **kwargs):
        # bump on the ORIGINAL condition, so a mutation that is never reached
        # scores zero instead of scoring silently.
        fired.bump_if(real(*args, **kwargs))
        return False

    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 2, 2, "    first = 'grew by a lot'")])["applied"]

    monkeypatch.setattr(citations, "chain_intact", disabled)
    blinded = apply(repo, [ref(cite, 5, 5, "    fourth = 44")])

    assert fired.count > 0, "the mutation never fired; this proves nothing"
    assert blinded["applied"] is False
    assert "changed since the digest was issued" in blinded["error"]
