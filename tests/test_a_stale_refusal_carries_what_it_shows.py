"""A stale citation is refused with the reread already done.

The refusal used to end "reread the range and cite the new digest". A round
that took that advice paid a turn, and a window of context, for text the
engine had in hand when it refused (the repeat-read pattern in
iss-53bf5bd892284b2c). `fresh` is that reread, inside the refusal.

Claims, each with its opposite pole:
  1. the text that is shown is where the cited text sits now, and its sha is
     good for a follow-up write with no read in between;
  2. nothing is shown for a range the journal says is gone, and no text is
     invented for it;
  3. an outside write is shown at the cited NUMBERS and labelled as such;
  4. the amount shown is bounded, and a sha never covers text that was not
     shown;
  5. the refusal's own wording, `applied`, and the file are exactly what they
     were: only `fresh` is new.
"""
import pytest

from skillflow import citations, read_accounting
from skillflow.read_tools import unified_read
from skillflow.strict_patch import MAX_FRESH_CHARS, apply_code_patch

RUN = "run-stale-refusal-fresh"
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


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def read(root, path="src/a.py", **kw):
    return unified_read(smap_for(root), path, run_id=RUN, **kw)


def apply(root, references):
    return apply_code_patch("", root, references=references, run_id=RUN)


def ref(cite, from_line, to_line, new_text, from_col=None, to_col=None, path="src/a.py"):
    body = {"file": path, "sha": cite["sha"], "from_line": from_line,
            "to_line": to_line, "new_text": new_text}
    if from_col is not None:
        body["from_col"] = from_col
    if to_col is not None:
        body["to_col"] = to_col
    return body


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def test_a_range_this_run_changed_is_shown_where_it_sits_now_and_citable(repo):
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 3, 3, "    second = 'replaced'")])["applied"]
    reads = read_accounting.summary(RUN)["reads"]

    # The caller cites lines 2-4, whose ends survived the edit but whose
    # middle did not.
    got = apply(repo, [ref(cite, 2, 4, "    gone")])
    assert got["applied"] is False and got["written"] == []
    fresh = got["fresh"]
    assert fresh["text"] == "    first = 1\n    second = 'replaced'\n    third = 3"
    assert (fresh["from_line"], fresh["to_line"]) == (2, 4)
    assert fresh["placement"] == "where the cited text sits now"
    assert fresh["sha"] and fresh["sha"] != cite["sha"]
    assert "cite `sha`" in fresh["note"]

    # No read call between the refusal and the write that follows it.
    again = apply(repo, [{"file": "src/a.py", "sha": fresh["sha"],
                          "from_line": 3, "to_line": 3,
                          "new_text": "    second = 'again'"}])
    assert again["applied"] is True, again
    assert read_accounting.summary(RUN)["reads"] == reads
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    second = 2", "    second = 'again'")


def test_an_insertion_inside_the_cited_range_is_shown_with_what_was_inserted(repo):
    cite = read(repo)["citation"]
    first = apply(repo, [ref(cite, 4, 4, "    inserted = 0\n", from_col=0, to_col=0)])
    assert first["applied"] is False, first
    spans = {span["reference"]: span for span in first["spans"]}
    assert apply(repo, [{**ref(cite, 4, 4, "    inserted = 0\n", from_col=0,
                               to_col=0), "sha": spans[1]["sha"]}])["applied"]

    got = apply(repo, [ref(cite, 3, 4, "    x\n    y")])
    assert got["applied"] is False
    assert "inserted text inside them" in got["error"]
    fresh = got["fresh"]
    assert fresh["text"] == "    second = 2\n    inserted = 0\n    third = 3"
    assert (fresh["from_line"], fresh["to_line"]) == (3, 5)
    assert fresh["sha"]


def test_a_line_this_run_replaced_shows_no_text(repo):
    """Re-citing the very line the run replaced: the journal cannot place the
    old text, and the caller already holds what it wrote in that apply's own
    `echo`. Nothing is invented for it."""
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 3, 3, "    second = 'replaced'")])["applied"]
    got = apply(repo, [ref(cite, 3, 3, "    second = 'again'")])
    assert got["applied"] is False
    assert got["fresh"]["text"] is None and "sha" not in got["fresh"]


def test_a_range_the_journal_says_is_gone_shows_no_text(repo):
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 3, 3, "")])["applied"]
    before = (repo / "src" / "a.py").read_bytes()

    got = apply(repo, [ref(cite, 3, 3, "    second = 'again'")])
    assert got["applied"] is False
    fresh = got["fresh"]
    assert fresh["text"] is None and "sha" not in fresh
    assert "removed or replaced" in fresh["reason"]
    assert "reread the range" in fresh["note"]
    assert (repo / "src" / "a.py").read_bytes() == before


def test_an_outside_write_is_shown_at_the_cited_numbers_and_labelled(repo):
    cite = read(repo)["citation"]
    (repo / "src" / "a.py").write_text(
        BODY.replace("    second = 2", "    second = 'outside'"))

    got = apply(repo, [ref(cite, 3, 3, "    second = 'mine'")])
    assert got["applied"] is False
    fresh = got["fresh"]
    assert fresh["text"] == "    second = 'outside'"
    assert (fresh["from_line"], fresh["to_line"]) == (3, 3)
    assert "same line numbers" in fresh["placement"]
    assert "cannot say whether that content moved" in fresh["placement"]
    assert apply(repo, [{"file": "src/a.py", "sha": fresh["sha"],
                         "from_line": 3, "to_line": 3,
                         "new_text": "    second = 'mine'"}])["applied"]


def test_the_text_shown_is_bounded_and_the_sha_covers_only_what_was_shown(repo):
    lines = [f"line {n:04d} " + "x" * 40 for n in range(1, 301)]
    (repo / "src" / "a.py").write_text("\n".join(lines) + "\n")
    cite = read(repo)["citation"]
    assert cite["end_line"] == 300
    (repo / "src" / "a.py").write_text("\n".join(lines).replace("line 0001", "LINE 0001") + "\n")

    got = apply(repo, [ref(cite, 1, 300, "gone")])
    assert got["applied"] is False
    fresh = got["fresh"]
    assert len(fresh["text"]) <= MAX_FRESH_CHARS
    shown_to = fresh["to_line"]
    assert 1 < shown_to < 300
    assert fresh["left_out"].startswith(f"lines {shown_to + 1}-300 were not shown")
    assert fresh["text"].splitlines()[0].startswith("LINE 0001")

    # A line past what was shown is outside the new citation.
    refused = apply(repo, [{"file": "src/a.py", "sha": fresh["sha"],
                            "from_line": shown_to + 1, "to_line": shown_to + 1,
                            "new_text": "nope"}])
    assert refused["applied"] is False
    assert "fall outside the cited window" in refused["error"]


def test_one_line_longer_than_the_cap_is_not_shown_at_all(repo):
    (repo / "src" / "a.py").write_text("short\n" + "y" * (MAX_FRESH_CHARS + 10) + "\n")
    cite = read(repo)["citation"]
    (repo / "src" / "a.py").write_text("short\n" + "z" * (MAX_FRESH_CHARS + 10) + "\n")

    got = apply(repo, [ref(cite, 2, 2, "x")])
    assert got["applied"] is False
    fresh = got["fresh"]
    assert fresh["text"] is None and "sha" not in fresh
    assert "longer than" in fresh["reason"]


def test_a_refusal_that_is_not_staleness_carries_no_fresh(repo):
    cite = read(repo)["citation"]
    got = apply(repo, [ref(cite, 3, 40, "x")])
    assert got["applied"] is False
    assert "fresh" not in got


def test_the_refusal_wording_and_the_file_are_exactly_what_they_were(repo):
    cite = read(repo)["citation"]
    assert apply(repo, [ref(cite, 3, 3, "    second = 'replaced'")])["applied"]
    before = (repo / "src" / "a.py").read_bytes()
    got = apply(repo, [ref(cite, 3, 3, "    second = 'again'")])
    assert got["error"].endswith("reread the range and cite the new digest")
    assert got["applied"] is False and got["partial"] is False
    assert got["written"] == [] and got["deleted"] == []
    assert (repo / "src" / "a.py").read_bytes() == before
