"""`file_exists` answers "did the step produce what it declared" — not "is it a file".

Live failure that motivated this. A `mode: write` step was told to produce a Python
package and produced it, correctly:

    B1.tmp/pyproject.toml
    B1.tmp/src/word_freq/{__init__,__main__,server}.py

Its validation declared `files: [src, src/word_freq, ...]`. Every one of those
existed. The check failed anyway — `passed = f.exists() and f.is_file()` — and the
message read "File not found: src (expected in .../B1.tmp). Files present:
pyproject.toml", because the sibling listing filtered out directories too. The agent
was told its directory was missing while it was sitting right there, rewrote the same
tree four times, and the run failed blaming the agent.

Worse, `files: ["*"]` had the same defect from the other side: `rglob("*")` yields
directories, each was checked with `is_file()`, so the canonical "assert this step
wrote SOMETHING" validation failed for any step that created a subdirectory.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))

from skillflow.tools.file_exists.impl import file_exists


def _tree(root: Path):
    (root / "pyproject.toml").write_text("[project]\n")
    (root / "src" / "word_freq").mkdir(parents=True)
    (root / "src" / "word_freq" / "server.py").write_text("def main(): ...\n")
    return root


class TestADeclaredDirectoryCounts:
    def test_a_populated_directory_passes(self, tmp_path):
        _tree(tmp_path)
        r = file_exists(["src", "src/word_freq"], workspace_root=str(tmp_path))
        assert r["all_passed"] is True, r["results"]

    def test_an_empty_directory_does_not(self, tmp_path):
        """The silent-wrote-nothing case wearing a directory as a disguise."""
        (tmp_path / "out").mkdir()
        r = file_exists(["out"], workspace_root=str(tmp_path))
        assert r["all_passed"] is False
        assert "empty directory" in r["results"][0]["error_message"]

    def test_a_nested_file_still_passes(self, tmp_path):
        _tree(tmp_path)
        r = file_exists(["src/word_freq/server.py"], workspace_root=str(tmp_path))
        assert r["all_passed"] is True

    def test_a_genuinely_missing_path_still_fails(self, tmp_path):
        _tree(tmp_path)
        r = file_exists(["README.md"], workspace_root=str(tmp_path))
        assert r["all_passed"] is False
        assert "Not found: README.md" in r["results"][0]["error_message"]


class TestTheStarPatternMeansSomethingWasWritten:
    def test_it_passes_when_the_step_created_subdirectories(self, tmp_path):
        """The regression that made every write-mode scaffolding step fail."""
        _tree(tmp_path)
        assert file_exists(["*"], workspace_root=str(tmp_path))["all_passed"] is True

    def test_it_passes_for_a_single_flat_file(self, tmp_path):
        (tmp_path / "a.md").write_text("x")
        assert file_exists(["*"], workspace_root=str(tmp_path))["all_passed"] is True

    def test_it_fails_when_nothing_was_written(self, tmp_path):
        r = file_exists(["*"], workspace_root=str(tmp_path))
        assert r["all_passed"] is False
        assert "Nothing matching '*'" in r["results"][0]["error_message"]

    def test_an_empty_directory_alone_is_not_something(self, tmp_path):
        (tmp_path / "out").mkdir()
        assert file_exists(["*"], workspace_root=str(tmp_path))["all_passed"] is False

    def test_a_narrower_glob_still_discriminates(self, tmp_path):
        _tree(tmp_path)
        assert file_exists(["*.py"], workspace_root=str(tmp_path))["all_passed"] is True
        assert file_exists(["*.rs"], workspace_root=str(tmp_path))["all_passed"] is False


class TestTheMessageShowsWhatIsActuallyThere:
    def test_directories_appear_in_the_listing(self, tmp_path):
        """The agent has to be able to SEE its own directory in the failure text."""
        _tree(tmp_path)
        msg = file_exists(["README.md"],
                          workspace_root=str(tmp_path))["results"][0]["error_message"]
        assert "src/" in msg and "pyproject.toml" in msg

    def test_an_empty_workspace_says_so(self, tmp_path):
        msg = file_exists(["a.md"],
                          workspace_root=str(tmp_path))["results"][0]["error_message"]
        assert "empty" in msg.lower()
