# tests/test_read_tools_exec.py
# Execution-level tests for generated read/search tool fns (read_tools.py):
# paging on read_{label}/read_{label}_file and search_{label} capabilities.

from skillflow.read_tools import make_read_tool_fns, generate_read_tool_schemas


def _mk_step(tmp_path, files):
    """Create a dpe_default step-2 dir with the given {name: body} files."""
    step = tmp_path / "dpe_default" / "2"
    step.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        fp = step / name
        fp.parent.mkdir(parents=True, exist_ok=True)
        fp.write_text(body)
    return tmp_path


_DIR_SPEC = {"source_type": "step", "step_id": "2"}


class TestReadDirFilePaging:
    def test_small_file_whole_not_truncated(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "L1\nL2\nL3"})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["read_step_2_file"](name="a.txt")
        assert r["total_lines"] == 3
        assert r["truncated"] is False
        assert "1\tL1" in r["content"] and "3\tL3" in r["content"]

    def test_large_file_paged_and_flagged(self, tmp_path):
        body = "\n".join(f"l{i}" for i in range(1, 5001))
        _mk_step(tmp_path, {"big.txt": body})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["read_step_2_file"](name="big.txt")
        assert r["total_lines"] == 5000
        assert r["truncated"] is True
        assert r["returned_lines"] == 2000  # _MAX_READ_LINES window

    def test_explicit_range_0_based(self, tmp_path):
        body = "\n".join(f"l{i}" for i in range(1, 101))
        _mk_step(tmp_path, {"big.txt": body})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["read_step_2_file"](name="big.txt", start_line=10, end_line=13)
        assert r["start_line"] == 10
        assert r["returned_lines"] == 3
        assert "11\tl11" in r["content"]  # 0-based start 10 → displayed line 11
        assert "14\tl14" not in r["content"]

    def test_missing_file_returns_error(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "x"})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["read_step_2_file"](name="nope.txt")
        assert "error" in r


class TestReadSingleFilePaging:
    def test_single_file_paged(self, tmp_path):
        body = "\n".join(f"l{i}" for i in range(1, 5001))
        _mk_step(tmp_path, {"design.md": body})
        spec = {"source_type": "step", "step_id": "2", "files": ["design.md"]}
        fns = make_read_tool_fns([spec], str(tmp_path), "dpe_default")
        r = fns["read_step_2_design"]()
        assert r["total_lines"] == 5000
        assert r["truncated"] is True
        assert r["returned_lines"] == 2000

    def test_single_file_whole(self, tmp_path):
        _mk_step(tmp_path, {"design.md": "one\ntwo"})
        spec = {"source_type": "step", "step_id": "2", "files": ["design.md"]}
        fns = make_read_tool_fns([spec], str(tmp_path), "dpe_default")
        r = fns["read_step_2_design"]()
        assert r["truncated"] is False
        assert "1\tone" in r["content"]


class TestReadSchemaParams:
    def test_dir_read_file_has_range_params(self, tmp_path):
        _mk_step(tmp_path, {"a.txt": "x"})
        schemas = generate_read_tool_schemas([_DIR_SPEC], str(tmp_path), "dpe_default")
        rf = next(s for s in schemas if s["name"] == "read_step_2_file")
        assert "start_line" in rf["parameters"]
        assert "end_line" in rf["parameters"]

    def test_single_read_has_range_params(self, tmp_path):
        _mk_step(tmp_path, {"design.md": "x"})
        spec = {"source_type": "step", "step_id": "2", "files": ["design.md"]}
        schemas = generate_read_tool_schemas([spec], str(tmp_path), "dpe_default")
        rf = next(s for s in schemas if s["name"] == "read_step_2_design")
        assert "start_line" in rf["parameters"]
        assert "end_line" in rf["parameters"]


class TestSearchDir:
    def test_finds_matches_with_line_and_text(self, tmp_path):
        _mk_step(tmp_path, {
            "a.py": "def foo():\n    return TARGET\n",
            "b.py": "x = 1\nTARGET = 2\n",
        })
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="TARGET")
        assert r["truncated"] is False
        files = {m["file"] for m in r["matches"]}
        assert files == {"a.py", "b.py"}
        assert all("line" in m and "text" in m for m in r["matches"])

    def test_glob_filter(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "TARGET\n", "notes.md": "TARGET\n"})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="TARGET", glob="*.py")
        assert {m["file"] for m in r["matches"]} == {"a.py"}

    def test_files_with_matches(self, tmp_path):
        _mk_step(tmp_path, {
            "a.py": "TARGET\nTARGET\n", "b.py": "TARGET\n", "c.py": "nope\n",
        })
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="TARGET", files_with_matches=True)
        assert "files" in r and "matches" not in r
        assert set(r["files"]) == {"a.py", "b.py"}  # deduped, one entry per file

    def test_context_lines(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "before\nHIT line\nafter\n"})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="HIT", context_lines=1)
        ctx = r["matches"][0]["context"]
        assert "before" in ctx and "after" in ctx

    def test_max_results_caps_and_flags(self, tmp_path):
        body = "\n".join("HIT" for _ in range(100))
        _mk_step(tmp_path, {"a.py": body})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="HIT", max_results=10)
        assert len(r["matches"]) == 10
        assert r["truncated"] is True

    def test_literal_fallback_on_bad_regex(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "cost = price * (1 + tax)\n"})
        fns = make_read_tool_fns([_DIR_SPEC], str(tmp_path), "dpe_default")
        r = fns["search_step_2"](pattern="(1 +")  # invalid regex → literal
        assert len(r["matches"]) == 1


class TestSearchSchemaParams:
    def test_search_schema_exposes_new_params(self, tmp_path):
        _mk_step(tmp_path, {"a.py": "x"})
        schemas = generate_read_tool_schemas([_DIR_SPEC], str(tmp_path), "dpe_default")
        s = next(x for x in schemas if x["name"] == "search_step_2")
        for p in ("pattern", "glob", "context_lines", "files_with_matches", "max_results"):
            assert p in s["parameters"]
