"""Tests for skillflow.tool_loader.ToolLoader."""

import pytest
from pathlib import Path
from skillflow.tool_loader import ToolLoader


@pytest.fixture
def tools_dir(tmp_path):
    """Create a temporary tools directory with two sample tools."""
    tools = tmp_path / "tools"

    # read_file tool
    read_dir = tools / "read_file"
    read_dir.mkdir(parents=True)
    (read_dir / "tool.yaml").write_text("""
name: read_file
description: Read a file from the project workspace
parameters:
  path:
    type: string
    description: Relative path from project root
    required: true
  start_line:
    type: integer
    required: false
""".strip())
    (read_dir / "impl.py").write_text("""
def read_file(path, start_line=0, end_line=None, *, workspace_root=""):
    full = __import__('pathlib').Path(workspace_root) / path
    if not full.exists():
        return {"error": f"File not found: {path}"}
    content = full.read_text(encoding="utf-8")
    lines = content.splitlines()
    if end_line is None:
        end_line = len(lines)
    result = lines[start_line:end_line]
    return {"content": "\\n".join(f"{start_line+i+1}\\t{line}" for i, line in enumerate(result)), "lines": len(result)}
""".strip())

    # echo tool (simple, no complex logic)
    echo_dir = tools / "echo"
    echo_dir.mkdir(parents=True)
    (echo_dir / "tool.yaml").write_text("""
name: echo
description: Echo back the input for testing
parameters:
  message:
    type: string
    required: true
""".strip())
    (echo_dir / "impl.py").write_text("""
def echo(message, *, workspace_root=""):
    return {"echo": message}
""".strip())

    # malformed tool: no export function
    bad_dir = tools / "bad_tool"
    bad_dir.mkdir(parents=True)
    (bad_dir / "tool.yaml").write_text("name: bad_tool")
    (bad_dir / "impl.py").write_text("# no function named bad_tool")

    return tools


class TestToolLoader:
    def test_load_schema(self, tools_dir):
        loader = ToolLoader(tools_dir)
        schema = loader.load_schema("read_file")
        assert schema["name"] == "read_file"
        assert "parameters" in schema
        assert schema["parameters"]["path"]["type"] == "string"

    def test_load_fn_and_call(self, tools_dir):
        loader = ToolLoader(tools_dir)
        fn = loader.load_fn("echo")
        result = fn("hello")
        assert result == {"echo": "hello"}

    def test_load_fn_read_file(self, tools_dir, tmp_path):
        # Create a test file
        test_file = tmp_path / "test.txt"
        test_file.write_text("line one\nline two\nline three")

        loader = ToolLoader(tools_dir)
        fn = loader.load_fn("read_file")
        result = fn("test.txt", workspace_root=str(tmp_path))
        assert result["lines"] == 3
        assert "line one" in result["content"]

    def test_load_fn_missing_export(self, tools_dir):
        loader = ToolLoader(tools_dir)
        with pytest.raises(ImportError, match="must export function"):
            loader.load_fn("bad_tool")

    def test_load_fn_nonexistent_tool(self, tools_dir):
        loader = ToolLoader(tools_dir)
        with pytest.raises(ImportError, match="not found"):
            loader.load_fn("nonexistent")

    def test_load_schema_nonexistent_tool(self, tools_dir):
        loader = ToolLoader(tools_dir)
        with pytest.raises(ImportError, match="not found"):
            loader.load_schema("nonexistent")

    def test_cache_reuse(self, tools_dir):
        loader = ToolLoader(tools_dir)
        fn1 = loader.load_fn("echo")
        fn2 = loader.load_fn("echo")
        assert fn1 is fn2  # Same object in cache

    def test_list_tools(self, tools_dir):
        loader = ToolLoader(tools_dir)
        names = loader.list_tools()
        assert "read_file" in names
        assert "echo" in names
        assert "bad_tool" in names
        # Only dirs with tool.yaml are listed
        assert len(names) == 3

class TestToolLoaderMultiSource:
    def test_multi_source_finds_in_second_dir(self, tmp_path):
        from skillflow.tool_loader import ToolLoader

        dir1 = tmp_path / "native"
        dir1.mkdir()
        dir2 = tmp_path / "custom"
        dir2.mkdir()
        (dir2 / "custom_tool").mkdir()
        (dir2 / "custom_tool" / "tool.yaml").write_text("name: custom_tool")
        (dir2 / "custom_tool" / "impl.py").write_text(
            "def custom_tool(*, workspace_root=''): return {'ok': True}")

        loader = ToolLoader(dir1, dir2)
        assert loader.list_tools() == ["custom_tool"]
        fn = loader.load_fn("custom_tool")
        assert fn() == {"ok": True}

    def test_add_tools_dir(self, tmp_path):
        from skillflow.tool_loader import ToolLoader

        dir1 = tmp_path / "native"
        dir1.mkdir()
        dir2 = tmp_path / "custom"
        dir2.mkdir()
        (dir2 / "echo").mkdir()
        (dir2 / "echo" / "tool.yaml").write_text("name: echo")
        (dir2 / "echo" / "impl.py").write_text(
            "def echo(*, workspace_root=''): return {'echo': 'hi'}")

        loader = ToolLoader(dir1)
        assert loader.list_tools() == []
        loader.add_tools_dir(dir2)
        assert loader.list_tools() == ["echo"]

    def test_first_match_wins_on_duplicate(self, tmp_path):
        from skillflow.tool_loader import ToolLoader

        d1 = tmp_path / "d1"
        d1.mkdir()
        (d1 / "dup").mkdir()
        (d1 / "dup" / "tool.yaml").write_text("name: dup\ndescription: from d1")
        (d1 / "dup" / "impl.py").write_text(
            "def dup(*, workspace_root=''): return {'from': 'd1'}")

        d2 = tmp_path / "d2"
        d2.mkdir()
        (d2 / "dup").mkdir()
        (d2 / "dup" / "tool.yaml").write_text("name: dup\ndescription: from d2")
        (d2 / "dup" / "impl.py").write_text(
            "def dup(*, workspace_root=''): return {'from': 'd2'}")

        loader = ToolLoader(d1, d2)
        fn = loader.load_fn("dup")
        assert fn() == {"from": "d1"}  # first dir wins

    def test_load_schema_from_second_dir(self, tmp_path):
        from skillflow.tool_loader import ToolLoader

        d1 = tmp_path / "d1"
        d1.mkdir()
        d2 = tmp_path / "d2"
        d2.mkdir()
        (d2 / "late").mkdir()
        (d2 / "late" / "tool.yaml").write_text("name: late\ndescription: found in d2")
        (d2 / "late" / "impl.py").write_text(
            "def late(*, workspace_root=''): return {}")

        loader = ToolLoader(d1, d2)
        schema = loader.load_schema("late")
        assert schema["name"] == "late"
        assert "found in d2" in schema["description"]
