import subprocess
"""Unit tests for skillflow/tools/ implementations."""

import json
import subprocess
import pytest
from pathlib import Path

# Tool impls
from skillflow.tools.read_file.impl import read_file
from skillflow.tools.write.impl import write
from skillflow.tools.list_tree.impl import list_tree
from skillflow.tools.dir_tree.impl import dir_tree
from skillflow.tools.json_schema.impl import json_schema
from skillflow.tools.repo_apply.impl import repo_apply
from skillflow.tools.lint.impl import lint
from skillflow.tools.pytest.impl import pytest as pytest_tool
from skillflow.tools.file_exists.impl import file_exists


class TestReadFile:
    def test_reads_file_with_line_numbers(self, tmp_path):
        (tmp_path / "test.txt").write_text("alpha\nbeta\ngamma")
        result = read_file("test.txt", workspace_root=str(tmp_path))
        assert result["total_lines"] == 3
        assert "1\talpha" in result["content"]
        assert "2\tbeta" in result["content"]

    def test_start_line_end_line(self, tmp_path):
        (tmp_path / "test.txt").write_text("a\nb\nc\nd\ne")
        result = read_file("test.txt", start_line=1, end_line=3,
                           workspace_root=str(tmp_path))
        assert result["returned_lines"] == 2
        assert "2\tb" in result["content"]

    def test_path_traversal_denied(self, tmp_path):
        result = read_file("../../etc/passwd", workspace_root=str(tmp_path))
        assert "error" in result

    def test_file_not_found(self, tmp_path):
        result = read_file("nonexistent.txt", workspace_root=str(tmp_path))
        assert "error" in result


class TestWrite:
    def test_writes_file(self, tmp_path):
        result = write("output.txt", "hello world",
                       workspace_root=str(tmp_path))
        assert result["written"] == "output.txt"
        assert (tmp_path / "output.txt").read_text() == "hello world"

    def test_path_traversal_denied(self, tmp_path):
        result = write("../../etc/passwd", "bad", workspace_root=str(tmp_path))
        assert "error" in result


class TestListTree:
    def test_lists_directory(self, tmp_path):
        (tmp_path / "src").mkdir()
        (tmp_path / "src" / "main.py").write_text("x")
        (tmp_path / "README.md").write_text("y")

        result = list_tree(".", workspace_root=str(tmp_path))
        assert "README.md" in result["tree"]
        assert "src/" in result["tree"]
        assert "main.py" in result["tree"]


class TestDirTree:
    def test_shows_project_and_workspace(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        (proj / "index.html").write_text("<html>")

        result = dir_tree(workspace_root=str(tmp_path),
                          project_root=str(proj))
        # AT-9: tree is rooted at ./ with a clarifying comment, NOT a bare
        # "project/" line that models mirror into write paths.
        assert "./" in result["tree"]
        assert "repo root" in result["tree"]
        assert "\nproject/" not in result["tree"]
        assert "index.html" in result["tree"]


class TestJsonSchema:
    def test_valid_passes(self, tmp_path):
        (tmp_path / "ok.json").write_text(
            json.dumps({"passed": True, "feedback": ""})
        )
        result = json_schema(
            files=["ok.json"],
            inline_schema={"required": ["passed", "feedback"]},
            workspace_root=str(tmp_path)
        )
        assert result["all_passed"] is True

    def test_missing_field_fails(self, tmp_path):
        (tmp_path / "bad.json").write_text(
            json.dumps({"foo": 1})
        )
        result = json_schema(
            files=["bad.json"],
            inline_schema={"required": ["passed"]},
            workspace_root=str(tmp_path)
        )
        assert result["all_passed"] is False


class TestRepoApply:
    def test_copies_files_and_commits(self, tmp_path):
        # Setup project repo
        proj = tmp_path / "project"
        proj.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=proj,
                       capture_output=True)
        subprocess.run(["git", "config", "user.email", "test@test.com"],
                       cwd=proj, capture_output=True)
        subprocess.run(["git", "config", "user.name", "Test"], cwd=proj,
                       capture_output=True)
        # Create a file for initial commit so git doesn't complain
        (proj / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "."], cwd=proj, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=proj,
                       capture_output=True)

        # Setup draft dir
        draft = tmp_path / "draft"
        draft.mkdir()
        (draft / "main.py").write_text("print('hello')")

        result = repo_apply(source_dir=str(draft),
                            workspace_root=str(tmp_path),
                            project_root=str(proj))
        assert result["applied"] is True
        assert "main.py" in result["files"]
        assert (proj / "main.py").read_text() == "print('hello')"

    def test_no_files(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        empty = tmp_path / "empty"
        empty.mkdir()

        result = repo_apply(source_dir=str(empty),
                            workspace_root=str(tmp_path),
                            project_root=str(proj))
        assert result["applied"] is False


class TestLint:
    """Tests for generic lint tool (replaces syntax_lint + py_compile)."""

    def _lint(self, tmp_path, pattern, **kw):
        """Helper: run lint with files=[pattern] and return first result."""
        r = lint([pattern], workspace_root=str(tmp_path), **kw)
        return r["all_passed"], r["results"]

    def test_valid_python_passes(self, tmp_path):
        (tmp_path / "ok.py").write_text("x = 1\ny = 2\nprint(x + y)\n")
        all_ok, results = self._lint(tmp_path, "ok.py")
        assert all_ok is True
        assert results[0]["passed"] is True

    def test_broken_python_fails(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(\n")
        all_ok, results = self._lint(tmp_path, "bad.py")
        assert all_ok is False

    def test_missing_file_skipped(self, tmp_path):
        all_ok, results = self._lint(tmp_path, "nonexistent.py")
        assert all_ok is True  # glob matches nothing → all passed

    def test_compile_check_fails(self, tmp_path):
        (tmp_path / "bad_syntax.py").write_text("def broken(\n")
        all_ok, results = self._lint(tmp_path, "bad_syntax.py")
        assert all_ok is False

    # ── HTML / Jinja2 ──

    def test_html_with_tag_passes(self, tmp_path):
        (tmp_path / "page.html").write_text(
            '<!DOCTYPE html>\n<html lang="en">\n<head><title>T</title></head>\n<body><p>hi</p></body>\n</html>')
        all_ok, _ = self._lint(tmp_path, "page.html")
        assert all_ok is True

    def test_jinja2_child_template_passes(self, tmp_path):
        """Jinja2 child templates with {% extends %} lack <html> tag — must pass."""
        (tmp_path / "child.html").write_text(
            '{% extends "base.html" %}\n{% block content %}<h1>hi</h1>{% endblock %}')
        all_ok, _ = self._lint(tmp_path, "child.html")
        assert all_ok is True

    def test_js_passes_with_content(self, tmp_path):
        (tmp_path / "app.js").write_text("function main() { console.log('hello'); }")
        all_ok, _ = self._lint(tmp_path, "app.js")
        assert all_ok is True

    def test_js_too_short_fails(self, tmp_path):
        (tmp_path / "tiny.js").write_text("x")
        all_ok, _ = self._lint(tmp_path, "tiny.js")
        assert all_ok is False

    def test_unknown_extension_skips(self, tmp_path):
        (tmp_path / "data.xyz").write_text("some content here")
        all_ok, results = self._lint(tmp_path, "data.xyz")
        assert all_ok is True
        assert "No linter configured" in results[0].get("error_message", "")

    def test_non_python_passes_py_compile(self, tmp_path):
        """Non-.py files should pass lint (only .py is checked by ruff)."""
        (tmp_path / "README.md").write_text("# README")
        all_ok, _ = self._lint(tmp_path, "README.md")
        assert all_ok is True

    def test_custom_manifest(self, tmp_path):
        """When manifest_path is given, it overrides defaults."""
        (tmp_path / "manifest.json").write_text('{".py": "basic"}')
        (tmp_path / "ok.py").write_text("print('hi')")
        all_ok, _ = self._lint(tmp_path, "ok.py", manifest_path="manifest.json")
        assert all_ok is True  # basic backend passes valid file

    def test_multiple_files_aggregate(self, tmp_path):
        (tmp_path / "a.py").write_text("print('ok')")
        (tmp_path / "b.py").write_text("def broken(\n")
        all_ok, results = self._lint(tmp_path, "*.py")
        assert all_ok is False
        assert len(results) == 2


class TestPytestTool:
    def test_passing_test_passes(self, tmp_path):
        (tmp_path / "test_pass.py").write_text(
            "def test_ok():\n    assert True\n"
        )
        result = pytest_tool("test_pass.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_failing_test_fails(self, tmp_path):
        (tmp_path / "test_fail.py").write_text(
            "def test_bad():\n    assert False\n"
        )
        result = pytest_tool("test_fail.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"

# ── Edge case tests ──

class TestJsonSchemaEdgeCases:
    def test_jsonschema_module_not_installed_fallback(self, tmp_path):
        """When jsonschema is not importable, basic required-field check is used."""
        (tmp_path / "ok.json").write_text('{"passed": true, "feedback": ""}')
        # Patch import to simulate missing jsonschema
        import builtins
        orig_import = builtins.__import__
        def mock_import(name, *args, **kwargs):
            if name == "jsonschema":
                raise ImportError("No module named 'jsonschema'")
            return orig_import(name, *args, **kwargs)
        builtins.__import__ = mock_import
        try:
            result = json_schema(
                files=["ok.json"],
                inline_schema={"required": ["passed"]},
                workspace_root=str(tmp_path)
            )
            assert result["all_passed"] is True
        finally:
            builtins.__import__ = orig_import

    def test_file_not_found_in_glob(self, tmp_path):
        result = json_schema(
            files=["nonexistent_*.json"],
            inline_schema={"required": ["x"]},
            workspace_root=str(tmp_path)
        )
        # No matching files → all_passed should be True (nothing to validate)
        assert result["all_passed"] is True

    def test_glob_finds_multiple_files(self, tmp_path):
        (tmp_path / "a.json").write_text('{"passed": true}')
        (tmp_path / "b.json").write_text('{"passed": true}')
        result = json_schema(
            files=["*.json"],
            inline_schema={"required": ["passed"]},
            workspace_root=str(tmp_path)
        )
        assert result["all_passed"] is True


class TestRepoApplyEdgeCases:
    def test_source_dir_relative_resolved(self, tmp_path):
        proj = tmp_path / "project"
        proj.mkdir()
        subprocess.run(["git", "init", "-b", "main"], cwd=proj, capture_output=True)
        subprocess.run(["git", "config", "user.email", "t@t.com"], cwd=proj, capture_output=True)
        subprocess.run(["git", "config", "user.name", "T"], cwd=proj, capture_output=True)
        (proj / ".gitkeep").write_text("")
        subprocess.run(["git", "add", "."], cwd=proj, capture_output=True)
        subprocess.run(["git", "commit", "-m", "init"], cwd=proj, capture_output=True)

        draft = tmp_path / "draft"
        draft.mkdir()
        (draft / "main.py").write_text("x=1")
        # Use relative path
        result = repo_apply(source_dir="draft", workspace_root=str(tmp_path),
                            project_root=str(proj))
        assert result["applied"] is True


class TestDirTreeEdgeCases:
    def test_handles_empty_dirs(self, tmp_path):
        result = dir_tree(workspace_root=str(tmp_path), project_root=str(tmp_path / "nonexistent"))
        assert "tree" in result


class TestListTreeEdgeCases:
    def test_path_traversal_denied(self, tmp_path):
        result = list_tree("../../etc", workspace_root=str(tmp_path))
        assert "error" in result

    def test_nonexistent_directory(self, tmp_path):
        result = list_tree("nonexistent", workspace_root=str(tmp_path))
        assert "error" in result


class TestPytestToolEdgeCases:
    def test_non_python_file_returns_passed(self, tmp_path):
        # README.md is not a .py file, pytest tool skips it
        (tmp_path / "README.md").write_text("# readme")
        result = pytest_tool("README.md", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"


class TestWritePathNormalization:
    """AT-9: collapse the phantom 'project/' root in write paths."""

    def test_generic_write_strips_project_prefix(self, tmp_path):
        from skillflow.write_tools import execute_generic_write, normalize_repo_path
        assert normalize_repo_path("project/strkit/core.py") == ["strkit", "core.py"]
        assert normalize_repo_path("strkit/core.py") == ["strkit", "core.py"]
        assert normalize_repo_path("core.py") == ["core.py"]
        # write lands at the collapsed path
        r = execute_generic_write({"file": "project/strkit/core.py", "content": "x=1"}, str(tmp_path))
        assert r["written"] == "strkit/core.py"
        assert (tmp_path / "strkit" / "core.py").read_text() == "x=1"

    def test_generic_write_dedup_collapses_to_one_file(self, tmp_path):
        from skillflow.write_tools import execute_generic_write
        execute_generic_write({"file": "project/strkit/core.py", "content": "A"}, str(tmp_path))
        execute_generic_write({"file": "strkit/core.py", "content": "B"}, str(tmp_path))
        # both resolve to the same path → one file, last wins
        assert (tmp_path / "strkit" / "core.py").read_text() == "B"
        assert not (tmp_path / "project").exists()

    def test_write_tool_strips_project_prefix(self, tmp_path):
        from skillflow.tools.write.impl import write
        r = write("project/pkg/m.py", "x=1", workspace_root=str(tmp_path))
        assert r["written"] == "pkg/m.py"
        assert (tmp_path / "pkg" / "m.py").exists()


class TestRepoApplyIdempotent:
    """AT-9 fallout: 'nothing to commit' is success, not a retry-triggering error."""

    def test_reapply_identical_is_success(self, tmp_path):
        import subprocess
        from skillflow.tools.repo_apply.impl import repo_apply
        repo = tmp_path / "repo"; repo.mkdir()
        subprocess.run(["git", "init", "-q"], cwd=repo)
        subprocess.run(["git", "config", "user.email", "t@t"], cwd=repo)
        subprocess.run(["git", "config", "user.name", "t"], cwd=repo)
        src = tmp_path / "src"; src.mkdir()
        (src / "a.py").write_text("x=1")
        r1 = repo_apply(source_dir=str(src), project_root=str(repo))
        assert r1["applied"] and r1.get("committed") is True
        # second identical apply → nothing to commit, still success
        r2 = repo_apply(source_dir=str(src), project_root=str(repo))
        assert r2["applied"] is True
        assert r2.get("committed") is False
        assert "error" not in r2


class TestPytestPackageImport:
    """AT-9 fallout: tests/test_x.py importing a repo-root package must resolve."""

    def test_repo_root_package_import_resolves(self, tmp_path):
        # repo_root/strkit/core.py + repo_root/tests/test_core.py importing it
        (tmp_path / "strkit").mkdir()
        (tmp_path / "strkit" / "__init__.py").write_text("")
        (tmp_path / "strkit" / "core.py").write_text("def reverse(s):\n    return s[::-1]\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_core.py").write_text(
            "from strkit.core import reverse\n"
            "def test_reverse():\n    assert reverse('ab') == 'ba'\n"
        )
        result = pytest_tool("tests/test_core.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed", result.get("feedback")

    def test_namespace_package_without_init_resolves(self, tmp_path):
        # PEP 420: package dir without __init__.py still imports if repo root on path
        (tmp_path / "strkit").mkdir()
        (tmp_path / "strkit" / "core.py").write_text("def f():\n    return 1\n")
        (tmp_path / "tests").mkdir()
        (tmp_path / "tests" / "test_core.py").write_text(
            "from strkit.core import f\n"
            "def test_f():\n    assert f() == 1\n"
        )
        result = pytest_tool("tests/test_core.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed", result.get("feedback")


class TestFileExists:
    """file_exists is used by graph validation specs to verify step outputs
    exist. If a required file is missing, it must return passed=False with
    an error_message — this is the signal that triggers step retry."""

    def test_existing_file_passes(self, tmp_path):
        (tmp_path / "step1_sota.md").write_text("# SOTA Report")
        result = file_exists(["step1_sota.md"], workspace_root=str(tmp_path))
        assert result["all_passed"] is True
        assert result["results"][0]["passed"] is True

    def test_missing_file_fails_with_message(self, tmp_path):
        result = file_exists(["step1_sota.md"], workspace_root=str(tmp_path))
        assert result["all_passed"] is False
        assert result["results"][0]["passed"] is False
        # SF-23: error message now includes expected path and directory context
        assert "File not found" in result["results"][0]["error_message"]
        assert "step1_sota.md" in result["results"][0]["error_message"]

    def test_multiple_files_mixed(self, tmp_path):
        (tmp_path / "existing.txt").write_text("ok")
        result = file_exists(
            ["existing.txt", "missing.txt"], workspace_root=str(tmp_path)
        )
        assert result["all_passed"] is False
        assert result["results"][0]["passed"] is True   # existing.txt
        assert result["results"][1]["passed"] is False  # missing.txt

    def test_glob_pattern(self, tmp_path):
        (tmp_path / "a.md").write_text("a")
        (tmp_path / "b.md").write_text("b")
        result = file_exists(["*.md"], workspace_root=str(tmp_path))
        assert result["all_passed"] is True
        assert len(result["results"]) == 2
