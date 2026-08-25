"""`create` must not report success for a zero-byte write, and a zero-byte file
must not be unrepairable.

Both were observed in one live run, in sequence, and together they killed a step:

  1. The agent called `create(file, new_str=<2.3k of JSON>)` -- the key its
     SIBLING tool `edit` takes. `content` was declared required in the schema
     but nothing enforced it at execution, so the executor defaulted it to "",
     wrote a zero-byte file, and returned `{"written": ...}`. The agent moved on
     believing it had delivered.
  2. Validation rejected the empty delivery and the step retried. Now the file
     EXISTED, so every subsequent `create` was refused with "already exists --
     use 'edit'", and every `edit` was refused with "'old_str' is required and
     must be non-empty" -- which an empty file cannot supply. There was no legal
     move. The agent oscillated between the two errors for ten turns and the
     step died with "No file writes produced after 10 tool exploration turn(s)",
     a message blaming the agent for a trap the tools had set.
"""

from pathlib import Path

from skillflow.write_tools import execute_generic_create, execute_generic_edit


def test_the_wrong_content_key_is_named_rather_than_written_as_empty(tmp_path):
    r = execute_generic_create({"file": "findings.json", "new_str": '{"a": 1}'},
                               str(tmp_path))
    assert "error" in r and "written" not in r
    assert "new_str" in r["error"], "the caller must be told WHICH key it used"
    assert not (tmp_path / "findings.json").exists(), "nothing may be written"


def test_an_empty_content_is_refused_not_reported_as_written(tmp_path):
    r = execute_generic_create({"file": "findings.json", "content": ""},
                               str(tmp_path))
    assert "error" in r and "written" not in r
    assert not (tmp_path / "findings.json").exists()


def test_a_real_create_still_works(tmp_path):
    r = execute_generic_create({"file": "a/b.json", "content": '{"ok": true}'},
                               str(tmp_path))
    assert r == {"written": str(Path("a/b.json"))}
    assert (tmp_path / "a" / "b.json").read_text() == '{"ok": true}'


def test_a_zero_byte_file_can_be_created_over(tmp_path):
    """The escape from the trap: there is nothing to clobber."""
    (tmp_path / "findings.json").write_text("")
    r = execute_generic_create({"file": "findings.json", "content": '{"findings": []}'},
                               str(tmp_path))
    assert r == {"written": "findings.json"}
    assert (tmp_path / "findings.json").read_text() == '{"findings": []}'


def test_a_zero_byte_file_in_the_REPO_does_not_block_either(tmp_path):
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    repo.mkdir(); out.mkdir()
    (repo / "findings.json").write_text("")
    r = execute_generic_create({"file": "findings.json", "content": "x"},
                               str(out), source_dir=str(repo))
    assert r == {"written": "findings.json"}


def test_a_file_with_real_content_is_still_protected(tmp_path):
    """The clobber guard is what create exists to enforce -- it must survive."""
    (tmp_path / "keep.py").write_text("important = 1\n")
    r = execute_generic_create({"file": "keep.py", "content": "gone"}, str(tmp_path))
    assert "already exists" in r.get("error", "")
    assert (tmp_path / "keep.py").read_text() == "important = 1\n"


def test_a_repo_file_with_content_is_still_protected(tmp_path):
    repo = tmp_path / "repo"
    out = tmp_path / "out"
    repo.mkdir(); out.mkdir()
    (repo / "keep.py").write_text("important = 1\n")
    r = execute_generic_create({"file": "keep.py", "content": "gone"},
                               str(out), source_dir=str(repo))
    assert "already exists" in r.get("error", "")


def test_the_deadlock_is_gone_end_to_end(tmp_path):
    """Replay the observed sequence: a bad create, then the repair."""
    # 1. the call that used to produce a zero-byte "success"
    bad = execute_generic_create({"file": "findings.json", "new_str": "{}"},
                                 str(tmp_path))
    assert "error" in bad

    # 2. even if a zero-byte file arrives some other way, edit cannot fix it...
    (tmp_path / "findings.json").write_text("")
    stuck = execute_generic_edit({"file": "findings.json", "old_str": "",
                                  "new_str": "{}"}, str(tmp_path))
    assert "old_str" in stuck.get("error", "")

    # ...so create must be the way out, and now it is.
    fixed = execute_generic_create({"file": "findings.json", "content": '{"findings": []}'},
                                   str(tmp_path))
    assert fixed == {"written": "findings.json"}
