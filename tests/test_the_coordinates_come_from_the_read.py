"""The range written is the range read: no coordinate the caller had to count.

A copied original is a checksum. The director measured it on 2026-09-21
(`~/.AItelier/director/reports/tocol-redundancy/`, skillflow 1.5.78): a V4A
hunk whose original is miscopied by one character is refused 4 times out of 4,
while a reference whose `to_col` is one short writes `DEFAULT_TIMEOUT_SECONDS =
450` and reports success. 27 and 28 are both legal columns in a 28-character
line, so no check the engine runs on the number can tell them apart.

It happened on the only real trial. Host run eac7cacb (trace ids 17 -> 22 -> 24)
cited the right sha on its first try and asked for line 1679 columns 0..27; the
line is 28 characters long. It noticed on turn 4, spent turns 5-9 counting, and
fixed it on turn 7 by abandoning references for a V4A retype (id 48).

Two changes, one per writing. The common intent needs no number: a reference
that carries only {file, sha, new_text} replaces the whole window the citation
was issued for, and one that names lines without columns replaces those whole
lines. The caller's belief about how long a line is has no field to travel in.

A column, when one is named, is no longer written on the caller's count. The
only way to have a column written is to be shown, by the engine, the exact text
those coordinates cover and what they leave on the line, and to cite back the
span citation the engine issued for exactly that range; the write is then bound
to those bytes. The call that counted 27 is refused and shown
`DEFAULT_TIMEOUT_SECONDS = 3` with `0` left behind, and nothing is written.
"""
import hashlib
import shutil
from pathlib import Path

import pytest

from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import PatchError, apply_code_patch, parse_references

RUN = "run-coordinates-from-the-read"
LINE = "DEFAULT_TIMEOUT_SECONDS = 30"
WANT = "DEFAULT_TIMEOUT_SECONDS = 45"
FILE = "app/settings.py"
# The director's probe body, byte for byte.
BODY = "import os\n\n%s\n\nDEBUG = False\n" % LINE
FIXTURE = Path(__file__).parent / "fixtures" / "eac7cacb_app_settings.py.txt"
# /tmp/refmode-3r_9rvh9/repo at HEAD 1674e5f, app/settings.py: the exact file
# host run eac7cacb edited.
FIXTURE_SHA256 = "036440eb37d4428cd72f43650cdb1750abdddd86f67b026610afa941fdac0ffe"


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def read(root, **kw):
    return unified_read(smap_for(root), FILE, run_id=RUN, **kw)


def apply(root, references):
    return apply_code_patch("", root, references=references, run_id=RUN)


def cite_span(root, reference):
    """The two-call column edit: be shown the span, then cite it."""
    before = (root / FILE).read_bytes()
    preview = apply(root, [reference])
    assert preview["applied"] is False, preview
    assert (root / FILE).read_bytes() == before
    (span,) = preview["spans"]
    return span, apply(root, [{**reference, "sha": span["sha"]}])


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "app").mkdir()
    (tmp_path / FILE).write_text(BODY)
    citations.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)


@pytest.fixture
def eac7cacb(tmp_path):
    assert hashlib.sha256(FIXTURE.read_bytes()).hexdigest() == FIXTURE_SHA256
    (tmp_path / "app").mkdir()
    shutil.copyfile(FIXTURE, tmp_path / FILE)
    citations.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)


# ===========================================================================
# replace-what-was-read-without-naming-a-column
# ===========================================================================

def test_citing_the_sha_alone_replaces_the_line_that_was_read(repo):
    """Pole 1: the real shape, and not one column computed anywhere."""
    served = read(repo, start_line=2, end_line=3)
    assert served["content"] == "3\t" + LINE
    cite = served["citation"]
    assert (cite["start_line"], cite["end_line"]) == (3, 3)
    ref = {"file": FILE, "sha": cite["sha"], "new_text": WANT}
    assert set(ref) == {"file", "sha", "new_text"}
    got = apply(repo, [ref])
    assert got["applied"] is True, got
    assert (repo / FILE).read_bytes() == (
        b"import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n")
    assert got["replaced"] == [{"file": FILE, "from_line": 3, "to_line": 3,
                                "replaced_chars": 28, "replaced": LINE}]


def test_a_narrowed_sub_range_is_still_honoured_as_written(repo):
    """Pole 2: adding the whole-window form must not take away the cut.

    Same file, same intent, two narrower writings: whole lines inside a wider
    window, and a column cut inside the line. Both land the same bytes.
    """
    want = b"import os\n\nDEFAULT_TIMEOUT_SECONDS = 45\n\nDEBUG = False\n"

    cite = read(repo)["citation"]
    assert (cite["start_line"], cite["end_line"]) == (1, 5)
    lines = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                          "to_line": 3, "new_text": WANT}])
    assert lines["applied"] is True, lines
    assert (repo / FILE).read_bytes() == want

    (repo / FILE).write_text(BODY)
    citations.forget_run(RUN)
    cite = read(repo)["citation"]
    span, cut = cite_span(repo, {"file": FILE, "sha": cite["sha"],
                                 "from_line": 3, "from_col": 26, "to_line": 3,
                                 "to_col": 28, "new_text": "45"})
    assert (span["text"], span["keeps_before"], span["keeps_after"]) == (
        "30", "DEFAULT_TIMEOUT_SECONDS = ", "")
    assert cut["applied"] is True, cut
    assert (repo / FILE).read_bytes() == want
    assert cut["replaced"][0]["replaced"] == "30"


def test_the_whole_window_is_exactly_the_window_and_nothing_either_side(repo):
    """A multi-line window: what was served goes, its neighbours stay."""
    cite = read(repo, start_line=1, end_line=4)["citation"]
    assert (cite["start_line"], cite["end_line"]) == (2, 4)
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "new_text": "X"}])
    assert got["applied"] is True, got
    assert (repo / FILE).read_bytes() == b"import os\nX\nDEBUG = False\n"
    assert got["replaced"][0]["from_line"] == 2
    assert got["replaced"][0]["to_line"] == 4
    assert got["replaced"][0]["replaced"] == "\n" + LINE + "\n"


def test_a_whole_window_survives_an_edit_elsewhere_in_the_file(repo):
    """The frozen frame applies to the column-free form too: an earlier write
    OUTSIDE the window moves the window's offsets, and the citation still
    lands on the text it was issued for."""
    whole = read(repo)["citation"]
    line3 = read(repo, start_line=2, end_line=3)["citation"]
    first = apply(repo, [{"file": FILE, "sha": whole["sha"], "from_line": 1,
                          "to_line": 1, "new_text": "import os, sys  # grew"}])
    assert first["applied"] is True, first
    second = apply(repo, [{"file": FILE, "sha": line3["sha"], "new_text": WANT}])
    assert second["applied"] is True, second
    assert (repo / FILE).read_text() == (
        "import os, sys  # grew\n\n" + WANT + "\n\nDEBUG = False\n")


def test_a_whole_window_whose_text_changed_is_refused_not_overwritten(repo):
    """The column-free form must not become a blind overwrite. A window this
    run already edited inside is no longer the text the caller was shown."""
    window = read(repo, start_line=1, end_line=4)["citation"]
    line3 = read(repo, start_line=2, end_line=3)["citation"]
    assert apply(repo, [{"file": FILE, "sha": line3["sha"],
                         "new_text": WANT}])["applied"] is True
    after = (repo / FILE).read_bytes()
    got = apply(repo, [{"file": FILE, "sha": window["sha"], "new_text": "X"}])
    assert got["applied"] is False
    assert "2-4" in got["error"]
    assert got["written"] == [] and got["partial"] is False
    assert (repo / FILE).read_bytes() == after

    # And a write from outside the engine to the cited line is refused too.
    fresh = read(repo, start_line=0, end_line=1)["citation"]
    outside = (repo / FILE).read_text().replace("import os", "import sys")
    (repo / FILE).write_text(outside)
    got = apply(repo, [{"file": FILE, "sha": fresh["sha"], "new_text": "Y"}])
    assert got["applied"] is False
    assert "changed since the digest was issued" in got["error"]
    assert (repo / FILE).read_text() == outside


@pytest.mark.parametrize("partial,message", [
    ({"from_line": 3}, "give both from_line and to_line"),
    ({"to_line": 3}, "give both from_line and to_line"),
    ({"to_col": 27}, "from_col/to_col need from_line and to_line"),
    ({"from_col": 0}, "from_col/to_col need from_line and to_line"),
])
def test_half_a_range_is_refused_rather_than_completed_by_guess(repo, partial,
                                                                message):
    cite = read(repo)["citation"]
    with pytest.raises(PatchError, match=message):
        parse_references([{"file": FILE, "sha": cite["sha"],
                           "new_text": WANT, **partial}])
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "new_text": WANT,
                        **partial}])
    assert got["applied"] is False and message in got["error"]
    assert (repo / FILE).read_text() == BODY


def test_the_sha_is_still_required_and_still_checked(repo):
    read(repo)
    with pytest.raises(PatchError, match=r"missing field\(s\) \['sha'\]"):
        parse_references([{"file": FILE, "new_text": WANT}])
    got = apply(repo, [{"file": FILE, "sha": "0" * 40, "new_text": WANT}])
    assert got["applied"] is False and "never issued" in got["error"]
    assert (repo / FILE).read_text() == BODY


# ===========================================================================
# a-mis-specified-range-cannot-write-silently
# ===========================================================================

def _probe_citation():
    """The director's probe issues its citation this way (strict path)."""
    lines = BODY.splitlines()
    return citations.issue(RUN, path=FILE, source="code", start_line=1,
                           end_line=len(lines), start_byte=0,
                           end_byte=len(BODY.encode()), text="\n".join(lines))


def test_the_len_minus_one_belief_has_no_field_to_travel_in(repo):
    """Pole 1. The caller that wrote to_col=27 believed the line was 27 long.
    In the new writing that belief is not sent: the request is the sha of the
    line the read served plus the new text, so `= 450` cannot be produced by
    it — there is no number in it to be one short."""
    served = read(repo, start_line=2, end_line=3)
    ref = {"file": FILE, "sha": served["citation"]["sha"], "new_text": WANT}
    assert not {"from_line", "to_line", "from_col", "to_col"} & set(ref)
    got = apply(repo, [ref])
    assert got["applied"] is True, got
    written = (repo / FILE).read_text().splitlines()[2]
    assert written == "DEFAULT_TIMEOUT_SECONDS = 45"
    assert written != "DEFAULT_TIMEOUT_SECONDS = 450"


def test_the_len_plus_one_error_is_still_loud_and_word_for_word(repo):
    """Pole 2. The polarity that already refused keeps refusing, with the
    message the director measured — not widened into an accept to make the
    two directions look alike."""
    cite = _probe_citation()
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                        "from_col": 0, "to_line": 3, "to_col": len(LINE) + 1,
                        "new_text": WANT}])
    assert got["applied"] is False
    assert ("to column 29 is past the end of line 3 (28 characters)"
            in got["error"])
    assert (repo / FILE).read_text() == BODY

    # Through a real read (the translated path) the same refusal says which
    # text it measured against.
    citations.forget_run(RUN)
    cite = read(repo)["citation"]
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                        "from_col": 0, "to_line": 3, "to_col": 29,
                        "new_text": WANT}])
    assert got["applied"] is False
    assert ("to column 29 is past the end of line 3 as the read served it "
            "(28 characters)" in got["error"])
    assert (repo / FILE).read_text() == BODY


NEW_ERROR = ("a column the caller counted has nothing to check it against, "
             "so columns must cite a span citation issued for exactly those "
             "coordinates. Nothing was written.")


@pytest.mark.parametrize("path", ["strict", "read"])
def test_the_old_uncorroborated_column_call_is_refused_with_the_new_error(
        repo, path):
    """The call eac7cacb made, as it made it, on both resolution paths: it is
    refused, nothing is written, and the refusal shows the caller the text its
    count covers and the digit its count leaves behind."""
    cite = _probe_citation() if path == "strict" else read(repo)["citation"]
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                        "from_col": 0, "to_line": 3, "to_col": 27,
                        "new_text": WANT}])
    assert got["applied"] is False
    assert got["written"] == [] and got["partial"] is False
    assert NEW_ERROR in got["error"]
    assert "'DEFAULT_TIMEOUT_SECONDS = 3'" in got["error"]
    assert "'0' after it" in got["error"]
    assert "omit from_col and to_col" in got["error"]
    (span,) = got["spans"]
    assert span["text"] == "DEFAULT_TIMEOUT_SECONDS = 3"
    assert (span["keeps_before"], span["keeps_after"]) == ("", "0")
    assert span["sha"] in got["error"]
    assert (repo / FILE).read_text() == BODY


def test_a_counted_whole_line_is_a_counted_column_too(repo):
    """to_col=28 is right here, but it is the same unchecked count as 27: it
    is refused the same way. The whole line is asked for by omitting it."""
    cite = read(repo)["citation"]
    got = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                        "from_col": 0, "to_line": 3, "to_col": 28,
                        "new_text": WANT}])
    assert got["applied"] is False and NEW_ERROR in got["error"]
    assert got["spans"][0]["text"] == LINE
    assert (repo / FILE).read_text() == BODY


def test_a_span_citation_corroborates_only_its_own_coordinates(repo):
    cite = read(repo)["citation"]
    ask = {"file": FILE, "sha": cite["sha"], "from_line": 3, "from_col": 0,
           "to_line": 3, "to_col": 27, "new_text": WANT}
    span = apply(repo, [ask])["spans"][0]
    for moved in ({"to_col": 28}, {"from_col": 1}, {"from_line": 2},
                  {"to_col": None}):
        got = apply(repo, [{**ask, "sha": span["sha"], **moved}])
        assert got["applied"] is False, moved
        assert "corroborates only the coordinates it was issued for" in got["error"]
    assert (repo / FILE).read_text() == BODY


def test_a_span_is_bound_to_the_bytes_it_showed(repo):
    """After the preview, the text under those coordinates changes: the span
    no longer describes it and the write is refused."""
    cite = read(repo)["citation"]
    ask = {"file": FILE, "sha": cite["sha"], "from_line": 3, "from_col": 26,
           "to_line": 3, "to_col": 28, "new_text": "45"}
    span = apply(repo, [ask])["spans"][0]
    outside = BODY.replace("= 30", "= 31")
    (repo / FILE).write_text(outside)
    got = apply(repo, [{**ask, "sha": span["sha"]}])
    assert got["applied"] is False
    assert "changed since its citation was issued" in got["error"]
    assert (repo / FILE).read_text() == outside


def test_a_span_survives_an_edit_elsewhere_and_its_sha_alone_is_enough(repo):
    cite = read(repo)["citation"]
    span = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 3,
                         "from_col": 26, "to_line": 3, "to_col": 28,
                         "new_text": "45"}])["spans"][0]
    grew = apply(repo, [{"file": FILE, "sha": cite["sha"], "from_line": 1,
                         "to_line": 1, "new_text": "import os, sys  # grew"}])
    assert grew["applied"] is True, grew
    got = apply(repo, [{"file": FILE, "sha": span["sha"], "new_text": "45"}])
    assert got["applied"] is True, got
    assert got["replaced"][0]["replaced"] == "30"
    assert (repo / FILE).read_text() == (
        "import os, sys  # grew\n\n" + WANT + "\n\nDEBUG = False\n")


# ===========================================================================
# prove-it-on-the-edit-that-actually-went-wrong
# ===========================================================================

def test_host_run_eac7cacb_exact_call_is_refused_not_written(eac7cacb):
    """Trace ids 16 and 22 as they were sent: the same read, then
    from_col=0, to_col=27 on line 1679. On 1.5.79 this wrote `= 450`."""
    original = (eac7cacb / FILE).read_bytes()
    cite = read(eac7cacb, start_line=1674, end_line=1685)["citation"]
    got = apply(eac7cacb, [{"file": FILE, "from_col": 0, "from_line": 1679,
                            "new_text": WANT, "sha": cite["sha"], "to_col": 27,
                            "to_line": 1679}])
    assert got["applied"] is False
    assert NEW_ERROR in got["error"]
    assert got["spans"][0]["text"] == "DEFAULT_TIMEOUT_SECONDS = 3"
    assert got["spans"][0]["keeps_after"] == "0"
    assert (eac7cacb / FILE).read_bytes() == original

def test_host_run_eac7cacb_replayed_writes_the_line_it_meant(eac7cacb):
    """Same file (sha256 pinned above), same read (trace id 16: start_line
    1674, end_line 1685), same line, same intent: the whole of line 1679
    becomes `= 45`, and nothing else in the 49,447-byte file moves."""
    original = (eac7cacb / FILE).read_bytes()
    served = read(eac7cacb, start_line=1674, end_line=1685)
    cite = served["citation"]
    # The window the agent was served (trace id 17), reproduced.
    assert (cite["start_line"], cite["end_line"]) == (1675, 1684)
    assert (cite["start_byte"], cite["end_byte"]) == (49155, 49447)
    assert cite["end_col"] == 19
    assert "1679\t" + LINE in served["content"]

    got = apply(eac7cacb, [{"file": FILE, "sha": cite["sha"], "from_line": 1679,
                            "to_line": 1679, "new_text": WANT}])
    assert got["applied"] is True, got
    after = (eac7cacb / FILE).read_bytes()
    assert after.decode().splitlines()[1678] == "DEFAULT_TIMEOUT_SECONDS = 45"
    # The plan's own verification: otherwise byte-identical.
    assert after == original.replace(b"\n" + LINE.encode() + b"\n",
                                     b"\n" + WANT.encode() + b"\n")
    assert original.count(LINE.encode()) == 1


def test_host_run_eac7cacb_replayed_with_the_sha_alone(eac7cacb):
    """The same edit with no number in the reference at all: read line 1679
    (0-based args, as the read tool takes them), cite its sha, give the text."""
    original = (eac7cacb / FILE).read_bytes()
    served = read(eac7cacb, start_line=1678, end_line=1679)
    assert served["content"] == "1679\t" + LINE
    got = apply(eac7cacb, [{"file": FILE, "sha": served["citation"]["sha"],
                            "new_text": WANT}])
    assert got["applied"] is True, got
    after = (eac7cacb / FILE).read_bytes()
    assert after.decode().splitlines()[1678] == "DEFAULT_TIMEOUT_SECONDS = 45"
    assert after == original.replace(LINE.encode(), WANT.encode())
