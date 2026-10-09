"""Synthetic source fixtures verify bounded disk-read echoes, partial writes
and the narrow refusal of multiline newline-less point inserts."""
import pytest

from skillflow import citations, read_accounting, strict_patch, write_tools
from skillflow.read_tools import unified_read
from skillflow.strict_patch import apply_code_patch

RUN = "run-echo-under-test"
PATH = "src/parts/example.gd"
# A synthetic six-line source with a blank line and a nonempty column-0 target.
BODY = "\n".join([
    "func _make_sample_rows_(options: Dictionary) -> Array:",   # 1
    "\tvar out: Array = []",                                     # 2
    "\tif state != \"\":",                                       # 3
    "",                                                          # 4
    "\t\tvar beat = _state_tick(state)",                         # 5
    "\treturn out",                                              # 6
]) + "\n"
# A multiline insertion without its final newline.
FUSING_TEXT = "\tvar state: String = String(options.get(\"state_id\", \"\"))\n\tif state != \"\":"


def smap_for(root):
    return {"working_tree": [("repo", str(root))], "named": {}, "allowed": set()}


# These claims are about what the echo reports (ranges, verbatim text, splices,
# partial publishes); they were written against the 1.5.83 geometry and keep
# it. The shipped geometry has its own claims in
# test_a_write_echo_shows_its_surroundings.py.
@pytest.fixture(autouse=True)
def _legacy_echo_geometry(monkeypatch):
    monkeypatch.setattr(strict_patch, "ECHO_HEAD", 20)
    monkeypatch.setattr(strict_patch, "ECHO_TAIL", 20)
    monkeypatch.setattr(strict_patch, "ECHO_CONTEXT", 1)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src" / "parts").mkdir(parents=True)
    (tmp_path / PATH).write_text(BODY)
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)
    yield tmp_path
    citations.forget_run(RUN)
    read_accounting.forget_run(RUN)


def cite(root):
    return unified_read(smap_for(root), PATH, run_id=RUN)["citation"]


def ref(sha, from_line, from_col, to_line, to_col, new_text):
    return {"file": PATH, "sha": sha, "from_line": from_line,
            "from_col": from_col, "to_line": to_line, "to_col": to_col,
            "new_text": new_text}


def apply(root, references=None, patch=""):
    before = {p: (root / p).read_bytes()
              for p in {PATH, *(r["file"] for r in references or [])}}
    for _ in range(len(references or []) + 1):
        result = apply_code_patch(patch, root, references=references, run_id=RUN)
        if not result.get("spans"):
            return result
        assert result["applied"] is False
        assert {p: (root / p).read_bytes() for p in before} == before
        confirmed = []
        for ref in references:
            span = next((s for s in result["spans"]
                         if all(s.get(k) == ref.get(k) for k in
                                ("file", "from_line", "from_col", "to_line", "to_col"))), None)
            confirmed.append({**ref, "sha": span["sha"]} if span else ref)
        references = confirmed
    raise AssertionError("span confirmation did not settle")


def shape(echo):
    """The echo as these tests pinned it, before each entry grew a citation
    (1.5.83): file, lines, text and elision. The citation has its own tests
    in test_a_write_hands_back_the_coordinates.py."""
    keep = ("file", "from_line", "to_line", "text", "elided", "error")
    return [{k: v for k, v in e.items() if k in keep} for e in echo]


def on_disk(root):
    return (root / PATH).read_text()


# ===========================================================================
# applied-edit-echoes-the-written-lines
# ===========================================================================

def test_the_fusing_reference_echoes_the_fused_line_verbatim(repo, monkeypatch):
    """The trace's own call, with the refusal switched off: the echo must show
    the seam the round could not see."""
    monkeypatch.setattr(strict_patch, "_refuse_fusing_insert",
                        lambda *args, **kwargs: None)
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 0, FUSING_TEXT)])
    assert got["applied"] is True, got
    fused = "\tif state != \"\":\tvar out: Array = []"
    assert fused in on_disk(repo).split("\n")
    assert shape(got["echo"]) == [{
        "file": PATH, "from_line": 2, "to_line": 3,
        "text": "\n".join([
            "1\tfunc _make_sample_rows_(options: Dictionary) -> Array:",
            "2\t\tvar state: String = String(options.get(\"state_id\", \"\"))",
            "3\t" + fused,
            "4\t\tif state != \"\":",
        ])}]


def test_the_echo_is_read_from_disk_after_the_write(repo, monkeypatch):
    """If the echo were rebuilt from the request it would say 41; the file
    says 42 because the writer (standing in for anything between the request
    and the disk) wrote 42."""
    real = write_tools._write_output_text

    def writer(target, text, **kwargs):
        return real(target, text.replace("= 41", "= 42"), **kwargs)

    monkeypatch.setattr(write_tools, "_write_output_text", writer)
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 20, "\tvar out = 41")])
    assert got["applied"] is True, got
    assert got["echo"][0]["text"].split("\n")[1] == "2\t\tvar out = 42"


def test_a_whole_line_replacement_echoes_that_line_and_its_neighbours(repo):
    got = apply(repo, [ref(cite(repo)["sha"], 3, 0, 3, 16, "\tif state == \"\":")])
    assert shape(got["echo"]) == [{"file": PATH, "from_line": 3, "to_line": 3,
                            "text": "2\t\tvar out: Array = []\n"
                                    "3\t\tif state == \"\":\n"
                                    "4\t"}]


def test_an_insert_ending_in_a_newline_echoes_the_new_line(repo):
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 0, "\tvar a = 1\n")])
    assert shape(got["echo"]) == [{"file": PATH, "from_line": 2, "to_line": 2,
                            "text": "1\tfunc _make_sample_rows_(options: "
                                    "Dictionary) -> Array:\n"
                                    "2\t\tvar a = 1\n"
                                    "3\t\tvar out: Array = []"}]


def test_every_reference_in_one_call_gets_its_own_resulting_range(repo):
    """The second range is stated in the RESULT's line numbers: the first
    edit grew the file by two lines above it."""
    sha = cite(repo)["sha"]
    got = apply(repo, [ref(sha, 5, 0, 5, 31, "\t\tvar beat = null"),
                       ref(sha, 2, 0, 2, 20, "\tvar out: Array = []\n\t# a\n\t# b")])
    assert got["applied"] is True, got
    assert [(e["from_line"], e["to_line"]) for e in got["echo"]] == [(2, 4), (7, 7)]
    assert got["echo"][1]["text"] == "6\t\n7\t\t\tvar beat = null\n8\t\treturn out"


def test_every_update_hunk_gets_its_own_resulting_range(repo):
    patch = "\n".join([
        "*** Begin Patch",
        f"*** Update File: {PATH}",
        "@@",
        " func _make_sample_rows_(options: Dictionary) -> Array:",
        "-\tvar out: Array = []",
        "+\tvar out: Array = []",
        "+\tvar extra := 0",
        "@@",
        " \t\tvar beat = _state_tick(state)",
        "-\treturn out",
        "+\treturn out.duplicate()",
        "*** End Patch",
    ])
    got = apply(repo, patch=patch)
    assert got["applied"] is True, got
    assert shape(got["echo"]) == [
        {"file": PATH, "from_line": 1, "to_line": 3,
         "text": "1\tfunc _make_sample_rows_(options: Dictionary) -> Array:\n"
                 "2\t\tvar out: Array = []\n3\t\tvar extra := 0\n"
                 "4\t\tif state != \"\":"},
        {"file": PATH, "from_line": 6, "to_line": 7,
         "text": "5\t\n6\t\t\tvar beat = _state_tick(state)\n"
                 "7\t\treturn out.duplicate()"},
    ]


def test_a_range_over_40_lines_shows_its_first_20_and_last_20(repo):
    block = "\n".join(f"\tvar v{i} = {i}" for i in range(50))
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 20, block)])
    assert got["applied"] is True, got
    (echo,) = got["echo"]
    assert (echo["from_line"], echo["to_line"], echo["elided"]) == (2, 51, 10)
    rows = echo["text"].split("\n")
    assert rows[0].startswith("1\tfunc ")
    assert rows[1:21] == [f"{2 + i}\t\tvar v{i} = {i}" for i in range(20)]
    assert rows[21] == "… 10 lines elided …"
    assert rows[22:42] == [f"{32 + i}\t\tvar v{30 + i} = {30 + i}" for i in range(20)]
    assert rows[42] == "52\t\tif state != \"\":"
    assert len(rows) == 43


def test_exactly_40_lines_are_shown_whole(repo):
    block = "\n".join(f"\tvar v{i} = {i}" for i in range(40))
    (echo,) = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 20, block)])["echo"]
    assert "elided" not in echo
    assert len(echo["text"].split("\n")) == 42


def test_a_refused_call_carries_no_echo(repo):
    got = apply(repo, [ref("0" * 40, 2, 0, 2, 20, "x")])
    assert got["applied"] is False and "echo" not in got


OTHER = "src/other.gd"


def fail_writing(monkeypatch, name):
    real = write_tools._write_output_text

    def writer(target, text, **kwargs):
        if target.name == name:
            raise OSError("injected disk error")
        return real(target, text, **kwargs)

    monkeypatch.setattr(write_tools, "_write_output_text", writer)


def test_a_partial_publish_echoes_the_files_it_wrote(repo, monkeypatch):
    """Files are published one at a time. When the second one fails, the
    first keeps its edit, and the result says what that file now reads; the
    file that failed is unchanged and has no echo."""
    (repo / OTHER).write_text("a\nb\n")
    here = cite(repo)["sha"]
    there = unified_read(smap_for(repo), OTHER, run_id=RUN)["citation"]["sha"]
    fail_writing(monkeypatch, "other.gd")
    got = apply(repo, [ref(here, 3, 0, 3, 16, "\tif state == \"\":"),
                       {**ref(there, 1, 0, 1, 1, "A"), "file": OTHER}])
    assert got["applied"] is False and got["partial"] is True, got
    assert got["phase"] == "publish" and got["written"] == [PATH]
    assert shape(got["echo"]) == [{"file": PATH, "from_line": 3, "to_line": 3,
                            "text": "2\t\tvar out: Array = []\n"
                                    "3\t\tif state == \"\":\n"
                                    "4\t"}]
    assert (repo / OTHER).read_text() == "a\nb\n"


def test_a_publish_that_fails_on_its_first_file_echoes_nothing(repo, monkeypatch):
    fail_writing(monkeypatch, "example.gd")
    got = apply(repo, [ref(cite(repo)["sha"], 3, 0, 3, 16, "\tif state == \"\":")])
    assert got["applied"] is False and got["partial"] is False, got
    assert got["phase"] == "publish" and got["written"] == []
    assert "echo" not in got and on_disk(repo) == BODY


# ===========================================================================
# multiline-point-insert-at-column-zero-is-refused
# ===========================================================================

def test_the_fusing_reference_is_refused_and_nothing_is_written(repo):
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 0, FUSING_TEXT)])
    assert got["applied"] is False, got
    assert got["phase"] == "preflight"
    assert got["written"] == []
    assert "(L,0)..(M,len(line M))" in got["error"]
    assert "(2,0)..(2,20)" in got["error"]
    assert on_disk(repo) == BODY


def test_the_refusal_is_atomic_for_the_whole_batch(repo):
    sha = cite(repo)["sha"]
    got = apply(repo, [ref(sha, 6, 0, 6, 11, "\treturn []"),
                       ref(sha, 2, 0, 2, 0, FUSING_TEXT)])
    assert got["applied"] is False and got["written"] == []
    assert on_disk(repo) == BODY


# The feasible cross product of the three properties the refusal reads:
# where the point is (column 0 of a nonempty line, column 0 of an empty line,
# a nonzero column) x what new_text is (one line, several lines not ending in
# a newline, several lines ending in one). Exactly one cell is refused.
POINTS = {
    "col0_nonempty": (2, 0),   # "\tvar out: Array = []"
    "col0_empty": (4, 0),      # ""
    "nonzero_col": (2, 1),     # after the tab
}
TEXTS = {
    "one_line": "# ",
    "multi_no_newline": "# a\n# b",
    "multi_newline": "# a\n# b\n",
}


@pytest.mark.parametrize("text_name", sorted(TEXTS))
@pytest.mark.parametrize("point_name", sorted(POINTS))
def test_the_insert_cross_product(repo, point_name, text_name):
    line, col = POINTS[point_name]
    new_text = TEXTS[text_name]
    got = apply(repo, [ref(cite(repo)["sha"], line, col, line, col, new_text)])
    refused = (point_name, text_name) == ("col0_nonempty", "multi_no_newline")
    if refused:
        assert got["applied"] is False, got
        assert on_disk(repo) == BODY
        return
    assert got["applied"] is True, got
    lines = BODY.split("\n")
    old = lines[line - 1]
    lines[line - 1] = old[:col] + new_text + old[col:]
    assert on_disk(repo) == "\n".join(lines)


def test_a_one_line_prefix_comments_a_line_out(repo):
    """(a) The legitimate column-0 point insert the refusal must not catch."""
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 0, "# ")])
    assert got["applied"] is True, got
    assert on_disk(repo).split("\n")[1] == "# \tvar out: Array = []"
    assert got["echo"][0]["text"].split("\n")[1] == "2\t# \tvar out: Array = []"


def test_a_crlf_new_text_is_judged_after_normalisation(repo):
    """new_text '\\r\\n'-terminated ends with a newline once normalised, so it
    is an insert, not the refused shape."""
    got = apply(repo, [ref(cite(repo)["sha"], 2, 0, 2, 0, "# a\r\n# b\r\n")])
    assert got["applied"] is True, got
