"""A successful write hands back the coordinates for the next one.

Measured on run 22ae403c (gen_director_source_impl, 2026-10-06): 82 reads,
30 successful writes; 19 reads directly followed a successful write and 61
re-served lines the run already had. The agent's own reasoning says why:
after a write it held a sha that answered to OLD line numbers while the
write's `echo` showed the region in NEW ones, with no sha — so it read.
Stale refusals, the thing a previous slice targeted, were 2 of 15 refusals.

Claims, each with its opposite pole:
  1. the echo of a write carries a citation over the lines it shows, in the
     file's current coordinates, and the next write in that region needs no
     read; the citation does not reach lines the echo did not show;
  2. an elided echo cites only its head and its tail, never the gap;
  3. a reference whose lines fall outside its cited window, or whose sha was
     never issued or is missing while file and lines are named, is refused
     with the current text of those lines and a sha for them;
  4. the refusals' wording, `applied` and the file bytes are what they were.
"""
import pytest

from skillflow import citations, read_accounting
from skillflow.read_tools import unified_read
from skillflow.strict_patch import ECHO_HEAD, ECHO_TAIL, apply_code_patch

RUN = "run-write-hands-back-coordinates"
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


def ref(sha, from_line, to_line, new_text, path="src/a.py", **cols):
    return {"file": path, "sha": sha, "from_line": from_line, "to_line": to_line,
            "new_text": new_text, **cols}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def text(repo):
    return (repo / "src" / "a.py").read_text()


# ===========================================================================
# 1. the echo is citable, in the new coordinates
# ===========================================================================

def test_the_echo_of_an_insertion_is_citable_at_its_new_line_numbers(repo):
    cite = read(repo)["citation"]
    reads = read_accounting.summary(RUN)["reads"]
    first = apply(repo, [ref(cite["sha"], 2, 2,
                             "    first = 1\n    inserted_a = 0\n    inserted_b = 0")])
    assert first["applied"] is True, first
    (echo,) = first["echo"]
    assert (echo["from_line"], echo["to_line"]) == (2, 4)
    citation = echo["citation"]
    # Context lines are shown, so they are cited too: 1..5 here.
    assert (citation["from_line"], citation["to_line"]) == (1, 5)
    assert citation["sha"] and citation["sha"] != cite["sha"]
    assert "cite this sha" in echo["note"]

    # NEW coordinates: `third = 3` is now line 6, outside the echo; `inserted_b`
    # is line 4. No read between the two writes.
    second = apply(repo, [ref(citation["sha"], 4, 4, "    inserted_b = 99")])
    assert second["applied"] is True, second
    assert read_accounting.summary(RUN)["reads"] == reads
    assert text(repo) == BODY.replace(
        "    first = 1", "    first = 1\n    inserted_a = 0\n    inserted_b = 99")


def test_the_echo_of_a_replacement_is_citable_and_the_old_sha_still_translates(repo):
    cite = read(repo)["citation"]
    first = apply(repo, [ref(cite["sha"], 3, 3, "    second = 'two'")])
    (echo,) = first["echo"]
    assert (echo["citation"]["from_line"], echo["citation"]["to_line"]) == (2, 4)
    second = apply(repo, [ref(echo["citation"]["sha"], 4, 4, "    third = 'three'")])
    assert second["applied"] is True, second
    # The journal recorded both writes: the ORIGINAL read's sha still lands.
    third = apply(repo, [ref(cite["sha"], 9, 9, "    return 1")])
    assert third["applied"] is True, third
    assert text(repo) == BODY.replace("    second = 2", "    second = 'two'").replace(
        "    third = 3", "    third = 'three'").replace("    return 0", "    return 1")


def test_the_echo_sha_does_not_reach_lines_the_echo_did_not_show(repo):
    cite = read(repo)["citation"]
    (echo,) = apply(repo, [ref(cite["sha"], 3, 3, "    second = 'two'")])["echo"]
    refused = apply(repo, [ref(echo["citation"]["sha"], 8, 8, "def helper2():")])
    assert refused["applied"] is False
    assert "fall outside the cited window 2-4" in refused["error"]


def test_without_a_run_the_echo_has_no_citation(repo):
    cite = read(repo)["citation"]
    got = apply_code_patch("", repo, references=[ref(cite["sha"], 3, 3, "x")], run_id="")
    # No run: the sha was never issued for it, so the write itself is refused,
    # and nothing below needs a run id to be well-formed.
    assert got["applied"] is False


# ===========================================================================
# 2. an elided echo cites only what it shows
# ===========================================================================

def test_an_elided_echo_cites_its_head_and_tail_and_not_the_gap(repo):
    cite = read(repo)["citation"]
    block = "\n".join(f"    line_{n:03d} = {n}" for n in range(ECHO_HEAD + ECHO_TAIL + 10))
    got = apply(repo, [ref(cite["sha"], 3, 3, block)])
    assert got["applied"] is True, got
    (echo,) = got["echo"]
    assert echo["elided"] == 10 and "citation" not in echo
    head, tail = echo["citations"]
    assert (head["from_line"], head["to_line"]) == (2, 2 + ECHO_HEAD)
    assert (tail["from_line"], tail["to_line"]) == (
        echo["to_line"] - ECHO_TAIL + 1, echo["to_line"] + 1)
    # Head and tail are both good for a write; a line in the gap is refused.
    assert apply(repo, [ref(head["sha"], 3, 3, "    line_000 = 'head'")])["applied"]
    assert apply(repo, [ref(tail["sha"], echo["to_line"], echo["to_line"],
                            "    line_049 = 'tail'")])["applied"]
    gap = apply(repo, [ref(head["sha"], 2 + ECHO_HEAD + 3, 2 + ECHO_HEAD + 3, "nope")])
    assert gap["applied"] is False and "fall outside the cited window" in gap["error"]


# ===========================================================================
# 3. refusals the engine cannot check carry the lines that were asked for
# ===========================================================================

def test_lines_outside_the_cited_window_come_back_with_a_sha_for_them(repo):
    cite = read(repo, start_line=1, end_line=4)["citation"]
    assert (cite["start_line"], cite["end_line"]) == (2, 4)
    got = apply(repo, [ref(cite["sha"], 8, 9, "def helper():\n    return 7")])
    assert got["applied"] is False
    assert "fall outside the cited window 2-4" in got["error"]
    fresh = got["fresh"]
    assert fresh["text"] == "def helper():\n    return 0"
    assert (fresh["from_line"], fresh["to_line"]) == (8, 9)
    assert "line numbers you asked for" in fresh["placement"]
    again = apply(repo, [ref(fresh["sha"], 8, 9, "def helper():\n    return 7")])
    assert again["applied"] is True, again


def test_a_sha_never_issued_comes_back_with_the_lines_it_named(repo):
    read(repo)
    got = apply(repo, [ref("deadbeef" * 5, 3, 3, "    second = 22")])
    assert got["applied"] is False and "was never issued" in got["error"]
    fresh = got["fresh"]
    assert fresh["text"] == "    second = 2" and fresh["sha"]
    assert apply(repo, [ref(fresh["sha"], 3, 3, "    second = 22")])["applied"]


def test_a_sha_never_issued_without_lines_carries_nothing(repo):
    read(repo)
    got = apply(repo, [{"file": "src/a.py", "sha": "deadbeef" * 5, "new_text": "x"}])
    assert got["applied"] is False and "was never issued" in got["error"]
    assert "fresh" not in got


def test_a_missing_sha_with_file_and_lines_comes_back_with_them(repo):
    got = apply(repo, [{"file": "src/a.py", "from_line": 4, "to_line": 5,
                        "new_text": "    third = 33\n    fourth = 44"}])
    assert got["applied"] is False
    assert got["error"].endswith("reference 1: missing field(s) ['sha']")
    fresh = got["fresh"]
    assert fresh["text"] == "    third = 3\n    fourth = 4"
    assert (fresh["from_line"], fresh["to_line"]) == (4, 5)
    again = apply(repo, [ref(fresh["sha"], 4, 5, "    third = 33\n    fourth = 44")])
    assert again["applied"] is True, again
    assert text(repo) == BODY.replace("    third = 3\n    fourth = 4",
                                      "    third = 33\n    fourth = 44")


def test_a_missing_sha_for_a_file_that_is_not_there_says_so(repo):
    got = apply(repo, [{"file": "src/nope.py", "from_line": 1, "to_line": 1,
                        "new_text": "x"}])
    assert got["applied"] is False and "missing field(s) ['sha']" in got["error"]
    assert got["fresh"]["text"] is None and "could not read" in got["fresh"]["reason"]


def test_a_missing_sha_without_lines_is_the_plain_refusal(repo):
    got = apply(repo, [{"file": "src/a.py", "new_text": "x"}])
    assert got["applied"] is False and "missing field(s) ['sha']" in got["error"]
    assert "fresh" not in got


def test_to_line_zero_is_told_what_an_insertion_point_is(repo):
    cite = read(repo)["citation"]
    got = apply(repo, [ref(cite["sha"], 3, 0, "x")])
    assert got["applied"] is False
    assert "require 1 <= from_line <= to_line" in got["error"]
    assert "from_col == to_col" in got["error"]


# ===========================================================================
# 4. nothing else moved
# ===========================================================================

def test_the_file_is_untouched_by_every_refusal_above(repo):
    read(repo, start_line=1, end_line=4)
    before = (repo / "src" / "a.py").read_bytes()
    cite = read(repo, start_line=1, end_line=4)["citation"]
    for refs in ([ref(cite["sha"], 8, 9, "x")],
                 [ref("deadbeef" * 5, 3, 3, "x")],
                 [{"file": "src/a.py", "from_line": 4, "to_line": 5, "new_text": "x"}]):
        got = apply(repo, refs)
        assert got["applied"] is False and got["written"] == [] and got["partial"] is False
    assert (repo / "src" / "a.py").read_bytes() == before
