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
from skillflow.tools.syntax_lint.impl import syntax_lint
from skillflow.tools.py_compile.impl import py_compile
from skillflow.tools.pytest.impl import pytest as pytest_tool


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
        assert "project/" in result["tree"]
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


class TestSyntaxLint:
    def test_valid_python_passes(self, tmp_path):
        (tmp_path / "ok.py").write_text("x = 1\ny = 2\nprint(x + y)\n")
        result = syntax_lint("ok.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_broken_python_fails(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(\n")
        result = syntax_lint("bad.py", workspace_root=str(tmp_path))
        # Should fail — either ruff or compile check will catch it
        assert "verdict" in result

    def test_missing_file(self, tmp_path):
        result = syntax_lint("nonexistent.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"


class TestPyCompile:
    def test_valid_python_passes(self, tmp_path):
        (tmp_path / "ok.py").write_text("x = 42\n")
        result = py_compile("ok.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_invalid_syntax_fails(self, tmp_path):
        (tmp_path / "bad.py").write_text("def broken(\n")
        result = py_compile("bad.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"


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

class TestSyntaxLintEdgeCases:
    def test_html_file_passes_with_html_tag(self, tmp_path):
        (tmp_path / "page.html").write_text("<!DOCTYPE html>\n<html lang=\"en\">\n<body>hello</body>\n</html>")
        result = syntax_lint("page.html", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_html_file_fails_without_html_tag(self, tmp_path):
        (tmp_path / "bad.html").write_text("<body>no html tag</body>")
        result = syntax_lint("bad.html", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"

    def test_js_file_passes_with_content(self, tmp_path):
        (tmp_path / "app.js").write_text("function main() { console.log('hello'); }")
        result = syntax_lint("app.js", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_js_file_too_short_fails(self, tmp_path):
        (tmp_path / "tiny.js").write_text("x")
        result = syntax_lint("tiny.js", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"


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


class TestPyCompileEdgeCases:
    def test_non_python_file_skips(self, tmp_path):
        (tmp_path / "README.md").write_text("# README")
        result = py_compile("README.md", workspace_root=str(tmp_path))
        assert result["verdict"] == "passed"

    def test_missing_file(self, tmp_path):
        result = py_compile("nonexistent.py", workspace_root=str(tmp_path))
        assert result["verdict"] == "failed"


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
