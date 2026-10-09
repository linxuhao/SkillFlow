"""A write's echo shows enough of the file around the edit to skip the next read.

Measured 2026-10-09 over recorded flash steps since 1.5.83 (ECHO_CONTEXT=1,
head/tail 20): of the first read of a file after its echo, 1% asked only for
lines the echo had shown; 55-62% asked for the echoed lines plus more around
them (median 11-18 extra lines, p75 26-43) and 5-6% for the elided middle.

Claims, each with its opposite pole:
  1. every echoed block carries ECHO_CONTEXT unchanged lines on each side,
     clipped at the ends of the file, numbered like a read;
  2. a touched range up to ECHO_HEAD + ECHO_TAIL lines is shown whole, a
     longer one still elides its middle;
  3. the echo citation covers exactly the shown lines, context included, so an
     edit inside the context needs no read; a line just beyond it is refused.
"""
import pytest

from skillflow import citations, read_accounting
from skillflow.read_tools import unified_read
from skillflow.strict_patch import ECHO_CONTEXT, ECHO_HEAD, ECHO_TAIL, apply_code_patch

RUN = "run-write-echo-shows-its-surroundings"
LINES = [f"line_{n:03d} = {n}" for n in range(1, 301)]   # 300 lines, line n says n
BODY = "\n".join(LINES) + "\n"


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


def read(root, **kw):
    return unified_read(smap_for(root), "src/a.py", run_id=RUN, **kw)


def apply(root, references):
    return apply_code_patch("", root, references=references, run_id=RUN)


def ref(sha, from_line, to_line, new_text):
    return {"file": "src/a.py", "sha": sha, "from_line": from_line, "to_line": to_line,
            "new_text": new_text}


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def numbers(echo):
    return [int(row.split("\t", 1)[0]) for row in echo["text"].splitlines()
            if row.split("\t", 1)[0].isdigit()]


def test_the_shipped_geometry_is_the_measured_one():
    assert (ECHO_CONTEXT, ECHO_HEAD, ECHO_TAIL) == (20, 40, 40)


def test_a_middle_edit_echoes_its_surroundings_on_both_sides(repo):
    cite = read(repo)["citation"]
    (echo,) = apply(repo, [ref(cite["sha"], 150, 150, "line_150 = 'changed'")])["echo"]
    assert (echo["from_line"], echo["to_line"]) == (150, 150)
    assert numbers(echo) == list(range(150 - ECHO_CONTEXT, 150 + ECHO_CONTEXT + 1))
    rows = echo["text"].splitlines()
    assert rows[0] == f"{150 - ECHO_CONTEXT}\tline_{150 - ECHO_CONTEXT:03d} = {150 - ECHO_CONTEXT}"
    assert f"150\tline_150 = 'changed'" in rows
    assert (echo["citation"]["from_line"], echo["citation"]["to_line"]) == (
        150 - ECHO_CONTEXT, 150 + ECHO_CONTEXT)


def test_the_context_is_clipped_at_both_ends_of_the_file(repo):
    cite = read(repo)["citation"]
    result = apply(repo, [ref(cite["sha"], 3, 3, "line_003 = 'top'"),
                          ref(cite["sha"], 298, 298, "line_298 = 'bottom'")])
    top, bottom = result["echo"]
    assert numbers(top) == list(range(1, 3 + ECHO_CONTEXT + 1))
    assert numbers(bottom) == list(range(298 - ECHO_CONTEXT, 301))


def test_a_range_up_to_head_plus_tail_lines_is_shown_whole(repo):
    cite = read(repo)["citation"]
    n = ECHO_HEAD + ECHO_TAIL
    new = "\n".join(f"new_{i} = {i}" for i in range(n))
    (echo,) = apply(repo, [ref(cite["sha"], 100, 100 + n - 1, new)])["echo"]
    assert "elided" not in echo
    assert numbers(echo) == list(range(100 - ECHO_CONTEXT, 100 + n + ECHO_CONTEXT))


def test_one_line_more_elides_the_middle(repo):
    cite = read(repo)["citation"]
    n = ECHO_HEAD + ECHO_TAIL + 1
    new = "\n".join(f"new_{i} = {i}" for i in range(n))
    (echo,) = apply(repo, [ref(cite["sha"], 100, 100 + n - 1, new)])["echo"]
    assert echo["elided"] == 1
    assert "… 1 lines elided …" in echo["text"]
    assert len(echo["citations"]) == 2


def test_an_edit_inside_the_context_needs_no_read_and_one_beyond_is_refused(repo):
    cite = read(repo)["citation"]
    reads = read_accounting.summary(RUN)["reads"]
    (echo,) = apply(repo, [ref(cite["sha"], 150, 150, "line_150 = 'changed'")])["echo"]
    sha = echo["citation"]["sha"]
    edge = 150 + ECHO_CONTEXT
    inside = apply(repo, [ref(sha, edge, edge, f"line_{edge:03d} = 'context edit'")])
    assert inside["applied"] is True, inside
    assert read_accounting.summary(RUN)["reads"] == reads
    (echo2,) = inside["echo"]
    beyond = apply(repo, [ref(echo2["citation"]["sha"], edge + ECHO_CONTEXT + 1,
                              edge + ECHO_CONTEXT + 1, "nope = 0")])
    assert beyond["applied"] is False
    assert "fall outside the cited window" in beyond["error"]
