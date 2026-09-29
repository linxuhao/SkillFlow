"""Reference hunks: cite the range the read served instead of copying it.

The shape this exists for, measured on attempt-c5aae131eaa246d0bc6d7c72864aea0f
(2026-09-20): 13 reads, 8 searches, 1 semantic_search, 0 writes, on a target
file of 44,602 characters against a 24,000-character read window. Only 3.9% of
that step's reasoning was original file text, so the cost was not transcription
— it was that "is my context still accurate?" had no machine answer and could
only be settled by reading again. A citation makes it a precondition the engine
checks.
"""
import hashlib

import pytest

from skillflow import citations
from skillflow.read_tools import unified_read
from skillflow.strict_patch import PatchError, apply_code_patch, parse_references

RUN = "run-under-test"
BODY = "\n".join([
    "def answer():",
    "    value = 1",
    "    other = 2",
    "    return value",
    "",
    "def helper():",
    "    return 0",
]) + "\n"


def smap_for(tmp_path):
    return {"working_tree": [("repo", str(tmp_path))], "named": {}, "allowed": set()}


def read(tmp_path, path="src/a.py", run_id=RUN, **kw):
    return unified_read(smap_for(tmp_path), path, run_id=run_id, **kw)


@pytest.fixture
def repo(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    return tmp_path


def apply(root, references=None, patch="", run_id=RUN):
    return apply_code_patch(patch, root, references=references, run_id=run_id)


def apply_corroborated(root, references, run_id=RUN):
    """Name columns, be shown what they cover, then cite that span.

    A reference that names a column is refused until it cites a span citation
    issued for exactly those coordinates; the refusal writes nothing and hands
    the span back. This is the two-call shape every column edit now has.
    """
    before = {p: p.read_bytes() for p in root.rglob("*") if p.is_file()}
    preview = apply(root, references, run_id=run_id)
    assert preview["applied"] is False, preview
    assert preview["written"] == [] and preview["partial"] is False
    assert {p: p.read_bytes() for p in root.rglob("*") if p.is_file()} == before
    spans = {span["reference"]: span for span in preview["spans"]}
    cited = [{**ref, "sha": spans[i]["sha"]} if i in spans else ref
             for i, ref in enumerate(references, 1)]
    return apply(root, cited, run_id=run_id)


# ===========================================================================
# the-digest-is-issued-by-the-read-not-computed-by-the-caller
# ===========================================================================

def test_the_read_reports_the_range_and_a_digest_for_it(repo):
    out = read(repo)
    cite = out["citation"]
    assert cite["path"] == "src/a.py"
    assert cite["source"] == "repo"
    assert (cite["start_line"], cite["end_line"]) == (1, 7)
    assert cite["start_byte"] == 0
    assert cite["end_byte"] == len(BODY.encode("utf-8"))
    assert cite["start_col"] == 0
    assert cite["citable"] is True
    assert len(cite["sha"]) == 40
    # The window, not the whole file, when the window is a window.
    part = read(repo, start_line=1, end_line=3)["citation"]
    assert (part["start_line"], part["end_line"]) == (2, 3)
    assert part["start_byte"] == len("def answer():\n".encode("utf-8"))
    assert part["sha"] != cite["sha"]


def test_a_digest_the_read_issued_applies(repo):
    cite = read(repo, start_line=1, end_line=2)["citation"]
    got = apply_corroborated(repo, [{"file": "src/a.py", "sha": cite["sha"],
                                     "from_line": 2, "from_col": 0,
                                     "to_line": 2, "to_col": len("    value = 1"),
                                     "new_text": "    value = 41"}])
    assert got["applied"] is True and got["written"] == ["src/a.py"]
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    value = 1", "    value = 41")


def test_a_correct_looking_digest_this_run_never_issued_is_refused(repo):
    """THE POINT OF THE CRITERION. Without this, reference mode degrades into
    editing coordinates the caller never actually read — worse than today."""
    window = "def answer():"
    out_of_band = [
        hashlib.sha256(window.encode()).hexdigest()[:40],          # plain digest
        hashlib.sha256(
            f"{RUN}\x00src/a.py\x00repo\x001\x001\x00{window}".encode()
        ).hexdigest()[:40],                                        # same recipe, no key
    ]
    # A digest issued by a DIFFERENT run is equally unissued here.
    citations.forget_run("other-run")
    other = citations.issue("other-run", path="src/a.py", source="repo",
                            start_line=1, end_line=1, start_byte=0,
                            end_byte=14, text=window)["sha"]
    out_of_band.append(other)
    before = (repo / "src" / "a.py").read_text()
    for sha in out_of_band:
        assert citations.lookup(RUN, sha) is None
        got = apply(repo, [{"file": "src/a.py", "sha": sha, "from_line": 1,
                            "from_col": 0, "to_line": 1, "to_col": 13,
                            "new_text": "def solved()"}])
        assert got["applied"] is False
        assert "never issued" in got["error"]
        assert got["written"] == [] and got["partial"] is False
    assert (repo / "src" / "a.py").read_text() == before


def test_the_caller_cannot_recompute_the_digest_it_was_given(repo):
    """The key is process-private and never emitted, so holding the original
    text is not enough to mint a digest — which is what stops a caller from
    citing a range it never read."""
    cite = read(repo, start_line=0, end_line=1)["citation"]
    assert cite["sha"] != hashlib.sha256("def answer():".encode()).hexdigest()[:40]
    assert cite["sha"] != hashlib.sha256(
        f"{RUN}\x00src/a.py\x00repo\x001\x001\x00def answer():".encode()
    ).hexdigest()[:40]


# ===========================================================================
# a-reference-hunk-never-restates-the-original
# ===========================================================================

def test_a_reference_hunk_has_nowhere_to_put_the_original(repo):
    accepted = {"file", "sha", "from_line", "from_col", "to_line", "to_col",
                "new_text"}
    cite = read(repo, start_line=1, end_line=2)["citation"]
    base = {"file": "src/a.py", "sha": cite["sha"], "from_line": 2,
            "from_col": 0, "to_line": 2, "to_col": 13, "new_text": "    x = 1"}
    assert set(base) == accepted
    for smuggled in ("old_text", "context", "old_str"):
        with pytest.raises(PatchError, match="only the NEW text"):
            parse_references([{**base, smuggled: "    value = 1"}])


def test_reference_and_v4a_produce_the_same_bytes(tmp_path):
    (tmp_path / "ref").mkdir()
    (tmp_path / "v4a").mkdir()
    for flavour in ("ref", "v4a"):
        (tmp_path / flavour / "src").mkdir()
        (tmp_path / flavour / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    cite = unified_read(
        {"working_tree": [("repo", str(tmp_path / "ref"))], "named": {},
         "allowed": set()},
        "src/a.py", start_line=1, end_line=4, run_id=RUN)["citation"]
    ref = apply_corroborated(tmp_path / "ref", [{
        "file": "src/a.py", "sha": cite["sha"],
        "from_line": 2, "from_col": 0,
        "to_line": 4, "to_col": len("    return value"),
        "new_text": "    value = 41\n    return value + 1"}])
    v4a = apply_code_patch(
        "*** Begin Patch\n*** Update File: src/a.py\n@@\n"
        "-    value = 1\n-    other = 2\n-    return value\n"
        "+    value = 41\n+    return value + 1\n*** End Patch\n",
        tmp_path / "v4a")
    assert ref["applied"] and v4a["applied"]
    assert (tmp_path / "ref" / "src" / "a.py").read_bytes() == \
           (tmp_path / "v4a" / "src" / "a.py").read_bytes()


def test_one_word_cites_one_line_and_leaves_the_rest_alone(repo):
    cite = read(repo, start_line=6, end_line=7)["citation"]
    assert cite["start_line"] == 7 and cite["end_line"] == 7
    got = apply_corroborated(repo, [{"file": "src/a.py", "sha": cite["sha"],
                                     "from_line": 7, "from_col": 11, "to_line": 7,
                                     "to_col": 12, "new_text": "7"}])
    assert got["applied"]
    assert (repo / "src" / "a.py").read_text() == BODY.replace(
        "    return 0", "    return 7")


def test_a_window_that_changed_since_the_digest_is_refused(repo):
    cite = read(repo, start_line=1, end_line=2)["citation"]
    (repo / "src" / "a.py").write_text(BODY.replace("    value = 1",
                                                    "    value = 9"))
    changed = (repo / "src" / "a.py").read_text()
    got = apply(repo, [{"file": "src/a.py", "sha": cite["sha"],
                        "from_line": 2, "from_col": 0, "to_line": 2,
                        "to_col": 13, "new_text": "    value = 41"}])
    assert got["applied"] is False
    assert "changed since the digest was issued" in got["error"]
    assert got["written"] == [] and got["partial"] is False
    assert (repo / "src" / "a.py").read_text() == changed


def test_a_range_outside_the_cited_window_is_refused(repo):
    cite = read(repo, start_line=0, end_line=2)["citation"]
    got = apply(repo, [{"file": "src/a.py", "sha": cite["sha"],
                        "from_line": 6, "from_col": 0, "to_line": 6,
                        "to_col": 3, "new_text": "xxx"}])
    assert got["applied"] is False and "outside the cited window" in got["error"]


def test_the_hunk_ceiling_applies_to_references_too(repo):
    cite = read(repo)["citation"]
    one = {"file": "src/a.py", "sha": cite["sha"], "from_line": 1,
           "from_col": 0, "to_line": 1, "to_col": 0, "new_text": "x"}
    assert len(parse_references([one] * 1024)) == 1
    with pytest.raises(PatchError, match="1024 reference hunks per file"):
        parse_references([one] * 1025)


def test_a_file_cannot_be_edited_by_both_modes_in_one_call(repo):
    cite = read(repo, start_line=0, end_line=1)["citation"]
    got = apply(repo,
                [{"file": "src/a.py", "sha": cite["sha"], "from_line": 1,
                  "from_col": 0, "to_line": 1, "to_col": 3, "new_text": "DEF"}],
                patch="*** Begin Patch\n*** Update File: src/a.py\n@@\n"
                      "-    other = 2\n+    other = 3\n*** End Patch\n")
    assert got["applied"] is False and "Duplicate operation" in got["error"]


# ===========================================================================
# one-snapshot-per-batch-and-the-engine-owns-the-order
# ===========================================================================

def _three_edits(tmp_path, order):
    (tmp_path / "src").mkdir(parents=True)
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    cite = unified_read(
        {"working_tree": [("repo", str(tmp_path))], "named": {},
         "allowed": set()}, "src/a.py", run_id=RUN)["citation"]
    edits = [
        {"file": "src/a.py", "sha": cite["sha"], "from_line": 2, "from_col": 4,
         "to_line": 2, "to_col": 13, "new_text": "value = 111"},
        {"file": "src/a.py", "sha": cite["sha"], "from_line": 4, "from_col": 4,
         "to_line": 4, "to_col": 16, "new_text": "return value * 2"},
        {"file": "src/a.py", "sha": cite["sha"], "from_line": 7, "from_col": 4,
         "to_line": 7, "to_col": 12, "new_text": "return 99"},
    ]
    got = apply_corroborated(tmp_path, [edits[i] for i in order])
    assert got["applied"] is True, got
    return (tmp_path / "src" / "a.py").read_bytes()


def test_unordered_references_match_descending_ones_byte_for_byte(tmp_path):
    descending = _three_edits(tmp_path / "desc", [2, 1, 0])
    scrambled = _three_edits(tmp_path / "mixed", [1, 2, 0])
    ascending = _three_edits(tmp_path / "asc", [0, 1, 2])
    assert descending == scrambled == ascending
    text = descending.decode()
    assert "    value = 111" in text
    assert "    return value * 2" in text
    assert "    return 99" in text
    # One snapshot: the second and third edits used coordinates from BEFORE the
    # first one lengthened its line, and nothing drifted.
    assert text.count("\n") == BODY.count("\n")


def test_overlapping_references_are_refused_without_writing(tmp_path):
    (tmp_path / "src").mkdir()
    (tmp_path / "src" / "a.py").write_text(BODY)
    citations.forget_run(RUN)
    cite = unified_read(
        {"working_tree": [("repo", str(tmp_path))], "named": {},
         "allowed": set()}, "src/a.py", run_id=RUN)["citation"]
    got = apply(tmp_path, [
        {"file": "src/a.py", "sha": cite["sha"], "from_line": 2, "from_col": 0,
         "to_line": 3, "to_col": 4, "new_text": "A"},
        {"file": "src/a.py", "sha": cite["sha"], "from_line": 3, "from_col": 0,
         "to_line": 4, "to_col": 4, "new_text": "B"},
    ])
    assert got["applied"] is False and "overlaps an earlier reference" in got["error"]
    assert got["written"] == [] and got["partial"] is False
    assert (tmp_path / "src" / "a.py").read_text() == BODY
